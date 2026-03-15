#!/usr/bin/env python3
"""
Orchestrator v4 — Sequential 4-agent pipeline for sensory data extraction.

File-based only (no DOI fetching). Download HTML/PDF manually, then:

    python scripts/orchestrate.py --file data/html/smith2019.html
    python scripts/orchestrate.py --file data/html/smith2019.html --doi "10.1093/chemse/28.3.219"
    python scripts/orchestrate.py --input-dir data/html/
    python scripts/orchestrate.py --file-list papers.csv

Pipeline per file:
    1. Detect file type (HTML/PDF) → parse article
    2. Generate paper_id, create extraction_run in SQLite
    3. Agent 1 — free extraction
    4. Agent 2 — structuring → commit to SQLite
    5. Agent 3 — figure extraction (vision, optional)
    6. Agent 4 — validation & correction
"""

import argparse
import csv
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.parse_article import detect_file_type, parse_article
from scripts.extract_figures import download_figures
from scripts.paper_id import doi_to_paper_id, paper_id_from_filename
from scripts.llm_extract import LLMClient
from scripts.init_db import init_database
from scripts.db import (
    get_db,
    create_extraction_run,
    update_extraction_run,
    update_paper_latest_run,
    delete_paper_data,
    get_paper,
)
from scripts.agent1_extract import run_agent1, save_agent1_output
from scripts.agent2_structure import run_agent2, commit_agent2_to_db, save_agent2_output
from scripts.agent3_figures import run_agent3, save_agent3_output
from scripts.agent4_validate import run_agent4, save_agent4_output

console = Console()

SUPPORTED_EXTENSIONS = {".html", ".htm", ".xhtml", ".xml", ".pdf"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config.yaml from project root."""
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Single-file pipeline
# ---------------------------------------------------------------------------

def _filter_figures_by_relevance(
    figure_metadata: list[dict],
    agent1_output: dict,
    threshold: float,
) -> tuple[list[dict], list[dict]]:
    """Split figure_metadata into (kept, skipped) based on Agent 1 relevance scores.

    Fail-open: figures with no matching score are kept.
    """
    # Build lookup: figure_id → inventory entry
    inventory = {
        entry.get("figure_id"): entry
        for entry in agent1_output.get("figure_inventory", [])
    }

    kept, skipped = [], []
    for fig in figure_metadata:
        fig_id = fig.get("figure_id", "")
        inv = inventory.get(fig_id)
        score = inv.get("relevance_score") if inv else None

        if score is not None and score < threshold:
            skipped.append({
                **fig,
                "relevance_score": score,
                "relevance_rationale": inv.get("relevance_rationale", ""),
            })
        else:
            kept.append(fig)

    return kept, skipped


def run_pipeline_from_file(
    file_path: Path,
    doi: str = "",
    study_id: str = "",
    config: dict | None = None,
    skip_figures: bool = False,
    force: bool = False,
    validate_only: bool = False,
    dry_run: bool = False,
    no_figure_filter: bool = False,
) -> dict:
    """Run the full 4-agent pipeline on a single local file.

    Returns a result dict with keys:
        paper_id, study_id, status ("ok" | "error" | "skipped"),
        agents_run, cost, error (if any).
    """
    config = config or load_config()
    file_path = Path(file_path).resolve()
    t0 = time.time()

    result = {
        "file": str(file_path),
        "paper_id": None,
        "study_id": study_id,
        "status": "ok",
        "agents_run": [],
        "cost": {},
        "error": None,
    }

    try:
        # ── 1. Detect file type ──────────────────────────────────────
        console.print(f"\n[bold cyan]▶ Processing:[/] {file_path.name}")
        file_type = detect_file_type(file_path)
        console.print(f"  File type: [green]{file_type}[/]")

        # Shared LLM client (tracks cost across all agents, including PDF table vision)
        llm = LLMClient(config) if not dry_run else None

        # ── 2. Parse article ────────────────────────────────────────────
        console.print("  Parsing article …")
        article = parse_article(
            file_path, file_type, doi=doi, study_id=study_id,
            config=config, llm=llm,
        )
        console.print(
            f"  Parsed: {len(article.sections)} sections, "
            f"{len(article.tables)} tables, {len(article.figures)} figures"
        )

        # Use DOI from parsed article if not provided explicitly
        if not doi and getattr(article, "doi", None):
            doi = article.doi

        # ── 3. Generate paper_id ────────────────────────────────────────
        if doi:
            paper_id = doi_to_paper_id(doi)
        else:
            paper_id = paper_id_from_filename(file_path.name)
        result["paper_id"] = paper_id

        if not study_id:
            study_id = paper_id
            result["study_id"] = study_id

        console.print(f"  Paper ID: [yellow]{paper_id}[/]")

        # ── Dry-run stops here ──────────────────────────────────────────
        if dry_run:
            console.print("  [dim]Dry run — skipping extraction.[/]")
            result["status"] = "skipped"
            return result

        # ── 4. Initialise DB & create extraction run ────────────────────
        init_database()
        conn = get_db(config)

        # Check for existing data
        existing = get_paper(conn, paper_id)
        if existing and not force and not validate_only:
            console.print(
                f"  [yellow]Paper already extracted (run {existing.get('latest_run_id')}). "
                f"Use --force to re-extract.[/]"
            )
            result["status"] = "skipped"
            conn.close()
            return result

        if force and existing:
            console.print("  [red]--force: deleting existing data …[/]")
            delete_paper_data(conn, paper_id)
            conn.commit()

        # Model & prompt versions for tracking
        model_versions = {
            "agent1": config.get("llm", {}).get("agent1_model", ""),
            "agent2": config.get("llm", {}).get("agent2_model", ""),
            "agent3": config.get("llm", {}).get("agent3_model", ""),
            "agent4": config.get("llm", {}).get("agent4_model", ""),
        }
        prompt_versions = config.get("prompt_versions", {})

        run_id = create_extraction_run(conn, paper_id, model_versions, prompt_versions)
        conn.commit()
        console.print(f"  Extraction run: [cyan]{run_id}[/]")

        # ── 5. Download / locate figures ────────────────────────────────
        figure_metadata = []
        if article.figures and not skip_figures:
            console.print(f"  Downloading {len(article.figures)} figures …")
            raw_figs = [
                {"url": fig.url, "caption": fig.caption, "figure_id": fig.figure_id}
                for fig in article.figures
            ]
            figure_metadata = download_figures(
                raw_figs, study_id, html_path=file_path
            )
            n_local = sum(1 for f in figure_metadata if f.get("local_path"))
            console.print(f"  Figures resolved: [green]{n_local}[/]/{len(figure_metadata)}")

        # ── Validate-only: skip agents 1-3, jump to agent 4 ────────────
        if validate_only:
            console.print("  [cyan]--validate-only: loading existing artifacts …[/]")
            extractions_dir = ROOT_DIR / config["paths"]["extractions_dir"]
            a1_path = extractions_dir / study_id / "agent1.json"
            a2_path = extractions_dir / study_id / "agent2.json"
            a3_path = extractions_dir / study_id / "agent3.json"

            if not a1_path.exists() or not a2_path.exists():
                raise FileNotFoundError(
                    f"Missing artifacts for --validate-only: need {a1_path} and {a2_path}"
                )

            with open(a1_path) as f:
                agent1_output = json.load(f)
            with open(a2_path) as f:
                agent2_output = json.load(f)
            agent3_output = None
            if a3_path.exists():
                with open(a3_path) as f:
                    agent3_output = json.load(f)

            # Jump to agent 4
            console.print("  [bold magenta]Agent 4[/] — Validation & correction …")
            agent4_output = run_agent4(
                article, agent1_output, agent2_output, agent3_output,
                paper_id, run_id, config, llm,
            )
            save_agent4_output(agent4_output, study_id, config)
            result["agents_run"].append("agent4")
            console.print("  Agent 4 ✓")

            # Update run record
            cost = llm.get_cost_summary()
            result["cost"] = cost
            update_extraction_run(
                conn, run_id,
                status="completed",
                validation_report=json.dumps(agent4_output),
                total_cost_usd=cost.get("total_cost", 0),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            update_paper_latest_run(conn, paper_id, run_id)
            conn.commit()
            conn.close()
            return result

        # ── 6. Agent 1 — Free extraction ───────────────────────────────
        console.print("  [bold magenta]Agent 1[/] — Free extraction …")
        agent1_output = run_agent1(article, study_id, config, llm)
        save_agent1_output(agent1_output, study_id, config)
        result["agents_run"].append("agent1")
        console.print("  Agent 1 ✓")

        # ── 7. Agent 2 — Structuring → SQLite ──────────────────────────
        console.print("  [bold magenta]Agent 2[/] — Structuring …")
        agent2_output = run_agent2(agent1_output, paper_id, config, llm)
        save_agent2_output(agent2_output, study_id, config)
        commit_agent2_to_db(agent2_output, paper_id, run_id, config)
        result["agents_run"].append("agent2")
        console.print("  Agent 2 ✓")

        # ── 7b. Filter figures by relevance ─────────────────────────────
        filtered_figures_info = []
        if figure_metadata and not skip_figures and not no_figure_filter:
            rel_threshold = config.get("figures", {}).get("relevance_threshold", 0.0)
            if rel_threshold > 0:
                figure_metadata, filtered_figures_info = _filter_figures_by_relevance(
                    figure_metadata, agent1_output, rel_threshold,
                )
                if filtered_figures_info:
                    console.print(
                        f"  Figures filtered: [green]{len(figure_metadata)} kept[/], "
                        f"[dim]{len(filtered_figures_info)} skipped (score < {rel_threshold})[/]"
                    )
                    for sf in filtered_figures_info:
                        console.print(
                            f"    [dim]↳ {sf.get('figure_id', '?')} "
                            f"(score={sf.get('relevance_score', '?')}: "
                            f"{sf.get('relevance_rationale', 'no rationale')})[/]"
                        )

        # ── 8. Agent 3 — Figure extraction (optional) ──────────────────
        agent3_output = None
        if figure_metadata and not skip_figures:
            console.print("  [bold magenta]Agent 3[/] — Figure extraction …")
            agent3_output = run_agent3(
                figure_metadata, agent1_output, agent2_output,
                paper_id, run_id, config, llm,
            )
            # Append filtered figures to Agent 3 output for audit trail
            if filtered_figures_info:
                agent3_output["filtered_figures"] = filtered_figures_info
            save_agent3_output(agent3_output, study_id, config)
            result["agents_run"].append("agent3")
            console.print("  Agent 3 ✓")
        elif skip_figures:
            console.print("  [dim]Agent 3 skipped (--skip-figures)[/]")
        else:
            console.print("  [dim]Agent 3 skipped (no figures)[/]")

        # ── 9. Agent 4 — Validation & correction ───────────────────────
        console.print("  [bold magenta]Agent 4[/] — Validation & correction …")
        agent4_output = run_agent4(
            article, agent1_output, agent2_output, agent3_output,
            paper_id, run_id, config, llm,
        )
        save_agent4_output(agent4_output, study_id, config)
        result["agents_run"].append("agent4")
        console.print("  Agent 4 ✓")

        # ── 10. Finalize extraction run ─────────────────────────────────
        cost = llm.get_cost_summary()
        result["cost"] = cost
        update_extraction_run(
            conn, run_id,
            status="completed",
            validation_report=json.dumps(agent4_output),
            total_cost_usd=cost.get("total_cost", 0),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        update_paper_latest_run(conn, paper_id, run_id)
        conn.commit()
        conn.close()

        elapsed = time.time() - t0
        console.print(
            f"  [bold green]✓ Done[/] in {elapsed:.1f}s — "
            f"${cost.get('total_cost', 0):.4f}"
        )

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        console.print(f"  [bold red]✗ Error:[/] {exc}")
        console.print(f"  [dim]{traceback.format_exc()}[/dim]")

    return result


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def _collect_files_from_dir(input_dir: Path) -> list[dict]:
    """Scan a directory for processable files. Returns list of job dicts."""
    jobs = []
    for p in sorted(input_dir.iterdir()):
        if p.suffix.lower() in SUPPORTED_EXTENSIONS and not p.name.startswith("."):
            jobs.append({"file_path": str(p), "doi": "", "study_id": ""})
    return jobs


def _collect_files_from_csv(csv_path: Path) -> list[dict]:
    """Read a CSV (columns: file_path, doi, study_id) into job dicts."""
    jobs = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fp = row.get("file_path", "").strip()
            if not fp:
                continue
            # Resolve relative paths against CSV location
            p = Path(fp)
            if not p.is_absolute():
                p = csv_path.parent / p
            jobs.append({
                "file_path": str(p),
                "doi": row.get("doi", "").strip(),
                "study_id": row.get("study_id", "").strip(),
            })
    return jobs


def _print_summary(results: list[dict]) -> None:
    """Print a rich summary table after batch processing."""
    table = Table(title="Extraction Summary", show_lines=True)
    table.add_column("File", style="cyan", max_width=40)
    table.add_column("Paper ID", style="yellow")
    table.add_column("Status", justify="center")
    table.add_column("Agents", style="magenta")
    table.add_column("Cost", justify="right", style="green")

    total_cost = 0.0
    ok = err = skipped = 0

    for r in results:
        fname = Path(r["file"]).name if r.get("file") else "?"
        pid = r.get("paper_id") or "—"
        status = r.get("status", "?")
        agents = ", ".join(r.get("agents_run", [])) or "—"
        cost_val = r.get("cost", {}).get("total_cost", 0)
        total_cost += cost_val

        if status == "ok":
            status_str = "[bold green]✓ ok[/]"
            ok += 1
        elif status == "skipped":
            status_str = "[yellow]⊘ skip[/]"
            skipped += 1
        else:
            status_str = f"[bold red]✗ {status}[/]"
            err += 1

        table.add_row(fname, pid, status_str, agents, f"${cost_val:.4f}")

    console.print()
    console.print(table)
    console.print(
        Panel(
            f"[green]{ok} succeeded[/]  "
            f"[yellow]{skipped} skipped[/]  "
            f"[red]{err} failed[/]  "
            f"[bold]Total cost: ${total_cost:.4f}[/]",
            title="Batch Results",
        )
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sensory data extraction pipeline v4 (file-based)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/orchestrate.py --file data/html/smith2019.html
  python scripts/orchestrate.py --file paper.pdf --doi "10.1093/chemse/28.3.219"
  python scripts/orchestrate.py --input-dir data/html/
  python scripts/orchestrate.py --file-list papers.csv
  python scripts/orchestrate.py --file paper.html --validate-only
        """,
    )

    # Input sources (mutually exclusive)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--file", type=Path,
        help="Single HTML or PDF file to process",
    )
    input_group.add_argument(
        "--input-dir", type=Path,
        help="Directory of HTML/PDF files to process",
    )
    input_group.add_argument(
        "--file-list", type=Path,
        help="CSV with columns: file_path, doi (optional), study_id (optional)",
    )

    # Metadata
    parser.add_argument(
        "--doi", type=str, default="",
        help="DOI for metadata (not used for fetching)",
    )
    parser.add_argument(
        "--study-id", type=str, default="",
        help="Custom study ID (default: derived from filename or DOI)",
    )

    # Behaviour flags
    parser.add_argument(
        "--skip-figures", action="store_true",
        help="Skip figure download and Agent 3 (vision extraction)",
    )
    parser.add_argument(
        "--no-figure-filter", action="store_true",
        help="Process ALL figures in Agent 3, ignoring relevance scores",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-extract even if output already exists",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Re-run Agent 4 (validation) without re-extracting",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without calling LLM APIs",
    )

    args = parser.parse_args()

    # ── Build job list ──────────────────────────────────────────────────
    jobs: list[dict] = []

    if args.file:
        if not args.file.exists():
            console.print(f"[bold red]File not found:[/] {args.file}")
            sys.exit(1)
        jobs.append({
            "file_path": str(args.file.resolve()),
            "doi": args.doi,
            "study_id": args.study_id,
        })

    elif args.input_dir:
        if not args.input_dir.is_dir():
            console.print(f"[bold red]Not a directory:[/] {args.input_dir}")
            sys.exit(1)
        jobs = _collect_files_from_dir(args.input_dir.resolve())
        if not jobs:
            console.print("[yellow]No processable files found in directory.[/]")
            sys.exit(0)
        console.print(f"Found [cyan]{len(jobs)}[/] files in {args.input_dir}")

    elif args.file_list:
        if not args.file_list.exists():
            console.print(f"[bold red]CSV not found:[/] {args.file_list}")
            sys.exit(1)
        jobs = _collect_files_from_csv(args.file_list.resolve())
        if not jobs:
            console.print("[yellow]No jobs found in CSV.[/]")
            sys.exit(0)
        console.print(f"Loaded [cyan]{len(jobs)}[/] jobs from {args.file_list}")

    # ── Load config once ────────────────────────────────────────────────
    config = load_config()

    # ── Header ──────────────────────────────────────────────────────────
    flags = []
    if args.skip_figures:
        flags.append("skip-figures")
    if args.force:
        flags.append("force")
    if args.validate_only:
        flags.append("validate-only")
    if args.dry_run:
        flags.append("dry-run")
    if args.no_figure_filter:
        flags.append("no-figure-filter")

    console.print(
        Panel(
            f"[bold]Sensory Extraction Pipeline v4[/]\n"
            f"Files: {len(jobs)}  "
            f"Flags: {', '.join(flags) or 'none'}",
            border_style="blue",
        )
    )

    # ── Process each file ───────────────────────────────────────────────
    results: list[dict] = []
    for i, job in enumerate(jobs, 1):
        console.rule(f"[bold]File {i}/{len(jobs)}")
        r = run_pipeline_from_file(
            file_path=job["file_path"],
            doi=job.get("doi", args.doi),
            study_id=job.get("study_id", args.study_id),
            config=config,
            skip_figures=args.skip_figures,
            force=args.force,
            validate_only=args.validate_only,
            dry_run=args.dry_run,
            no_figure_filter=args.no_figure_filter,
        )
        results.append(r)

    # ── Summary ─────────────────────────────────────────────────────────
    if len(results) > 1 or any(r["status"] == "error" for r in results):
        _print_summary(results)
    elif results and results[0]["status"] == "ok":
        cost = results[0].get("cost", {})
        console.print(
            Panel(
                f"[bold green]Pipeline complete[/] — "
                f"paper_id: {results[0]['paper_id']}\n"
                f"Agents: {', '.join(results[0].get('agents_run', []))}\n"
                f"Total cost: ${cost.get('total_cost', 0):.4f}",
                border_style="green",
            )
        )

    # Exit with error code if any failures
    if any(r["status"] == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
