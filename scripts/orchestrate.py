#!/usr/bin/env python3
"""
Orchestrator: Single-command pipeline for sensory data extraction.

Usage:
    python scripts/orchestrate.py --doi "10.3390/nu10111632"
    python scripts/orchestrate.py --doi "10.3390/nu10111632" --study-id wee2018
    python scripts/orchestrate.py --doi-list papers.csv
    python scripts/orchestrate.py --doi "10.xxxx/yyyy" --skip-figures
    python scripts/orchestrate.py --doi "10.xxxx/yyyy" --dry-run
    python scripts/orchestrate.py --doi "10.xxxx/yyyy" --validate
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

console = Console()


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def run_pipeline(doi: str, study_id: str = "", config: dict = None,
                 skip_figures: bool = False, force: bool = False,
                 validate: bool = False, dry_run: bool = False) -> dict:
    """Run the full extraction pipeline for a single paper.

    Steps:
        1. Resolve DOI → publisher
        2. Fetch HTML/XML
        3. Parse → sections, tables, figure URLs
        4. Download figures
        5. Run Prompts A-D (Sonnet)
        6. Run Prompt E (Opus) for each figure
        7. Assemble complete JSON
        8. Normalize attributes
        9. Build/update SQLite index row
        10. Flag data gaps

    Returns:
        Dict with pipeline results and status.
    """
    if config is None:
        config = load_config()

    result = {
        "doi": doi,
        "study_id": study_id,
        "status": "pending",
        "steps_completed": [],
        "errors": [],
        "start_time": datetime.now().isoformat(),
    }

    try:
        # ── Step 1: Resolve DOI ──────────────────────────────────
        console.print(f"\n[bold blue]📄 Processing: {doi}[/bold blue]")

        if dry_run:
            console.print("[yellow]DRY RUN — no API calls will be made[/yellow]")

        console.print("  [dim]Step 1/10: Resolving DOI...[/dim]")
        from scripts.fetch_article import resolve_doi
        article_info = resolve_doi(doi)
        study_id = study_id or article_info["doi"].split("/")[-1].replace(".", "").lower()
        result["study_id"] = study_id
        result["publisher"] = article_info["publisher"]
        result["title"] = article_info.get("title", "")
        result["steps_completed"].append("resolve_doi")
        console.print(f"  Publisher: {article_info['publisher']} | {article_info.get('title', '')[:60]}")

        if dry_run:
            result["status"] = "dry_run_complete"
            return result

        # ── Step 2: Fetch HTML/XML ───────────────────────────────
        console.print("  [dim]Step 2/10: Fetching article...[/dim]")
        html_dir = ROOT_DIR / config["paths"]["html_dir"]
        html_path = html_dir / f"{study_id}.html"

        if html_path.exists() and not force:
            console.print(f"  [green]Already fetched: {html_path.name}[/green]")
        else:
            from scripts.fetch_article import fetch_html
            html_path = fetch_html(
                doi=doi,
                publisher=article_info["publisher"],
                url=article_info["url"],
                output_dir=html_dir,
                study_id=study_id,
            )
        result["html_path"] = str(html_path)
        result["steps_completed"].append("fetch_html")

        # ── Step 3: Parse article ────────────────────────────────
        console.print("  [dim]Step 3/10: Parsing article...[/dim]")
        from scripts.parse_article import parse_article
        article = parse_article(html_path, article_info["publisher"], doi, study_id)
        result["steps_completed"].append("parse_article")
        result["num_tables"] = len(article.tables)
        result["num_figures"] = len(article.figures)

        # ── Step 4: Download figures ─────────────────────────────
        figure_metadata = []
        if not skip_figures and article.figures:
            console.print(f"  [dim]Step 4/10: Downloading {len(article.figures)} figures...[/dim]")
            from scripts.extract_figures import download_figures
            figure_metadata = download_figures(article.figures, study_id)
            result["steps_completed"].append("download_figures")
        else:
            console.print("  [dim]Step 4/10: Skipping figures[/dim]")
            result["steps_completed"].append("skip_figures")

        # ── Step 5-8: LLM Extraction ────────────────────────────
        console.print("  [dim]Step 5/10: Running LLM extraction (Prompt A: Metadata)...[/dim]")
        from scripts.llm_extract import LLMClient, load_prompt, format_gold_example

        llm = LLMClient(config)
        parts_dir = ROOT_DIR / config["paths"]["extractions_dir"] / "parts" / study_id
        parts_dir.mkdir(parents=True, exist_ok=True)

        # Load gold-standard examples for few-shot prompts
        try:
            wee_example = format_gold_example("wee2018")
            benabu_example = format_gold_example("benabu2018")
        except FileNotFoundError:
            wee_example = "{}"
            benabu_example = "{}"
            console.print("  [yellow]⚠ Gold standard files not found — running without few-shot examples[/yellow]")

        # Prompt A: Study Metadata
        metadata_result = _run_prompt_a(llm, article, wee_example, benabu_example)
        _save_part(parts_dir / "metadata.json", metadata_result)
        result["steps_completed"].append("prompt_a")

        # Prompt B: Experiment Design
        console.print("  [dim]Step 6/10: Running Prompt B: Experiment Design...[/dim]")
        experiment_result = _run_prompt_b(llm, article, study_id, wee_example, benabu_example)
        _save_part(parts_dir / "experiments.json", experiment_result)
        result["steps_completed"].append("prompt_b")

        # Prompt C: Stimuli
        console.print("  [dim]Step 7/10: Running Prompt C: Stimuli...[/dim]")
        stimuli_result = _run_prompt_c(llm, article, study_id, wee_example, benabu_example)
        _save_part(parts_dir / "stimuli.json", stimuli_result)
        result["steps_completed"].append("prompt_c")

        # Prompt D: Sensory Data
        console.print("  [dim]Step 8/10: Running Prompt D: Sensory Data...[/dim]")
        sensory_result = _run_prompt_d(llm, article, study_id, wee_example, benabu_example)
        _save_part(parts_dir / "sensory_data.json", sensory_result)
        result["steps_completed"].append("prompt_d")

        # Prompt E: Figure Extraction (optional)
        figure_extractions = []
        if not skip_figures and figure_metadata:
            console.print(f"  [dim]Step 8b: Running Prompt E: Figure Extraction ({len(figure_metadata)} figures)...[/dim]")
            figures_parts_dir = parts_dir / "figures"
            figures_parts_dir.mkdir(exist_ok=True)

            for fig in figure_metadata:
                if fig.get("local_path") and Path(fig["local_path"]).exists():
                    try:
                        fig_result = _run_prompt_e(
                            llm, fig, article, study_id, wee_example
                        )
                        figure_extractions.append(fig_result)
                        _save_part(
                            figures_parts_dir / f"{fig['figure_id']}.json", fig_result
                        )
                    except Exception as e:
                        console.print(f"    [red]✗ Figure {fig['figure_id']}: {e}[/red]")
                        result["errors"].append(f"Figure extraction failed: {fig['figure_id']}: {e}")

            result["steps_completed"].append("prompt_e")

        # ── Step 9: Assemble JSON ───────────────────────────────
        console.print("  [dim]Step 9/10: Assembling complete JSON...[/dim]")
        from scripts.assemble_json import assemble_json, save_json

        paper = assemble_json(
            study_id=study_id,
            metadata=metadata_result,
            experiments_design=experiment_result,
            stimuli=stimuli_result,
            sensory_data=sensory_result,
            figure_data=figure_extractions,
            figure_inventory=figure_metadata,
        )

        # Normalize attributes
        from scripts.normalize_attributes import normalize_attributes
        paper, new_mappings = normalize_attributes(paper, interactive=False)

        output_path = save_json(paper)
        result["output_path"] = str(output_path)
        result["steps_completed"].append("assemble")

        # ── Step 10: Update SQLite index ─────────────────────────
        console.print("  [dim]Step 10/10: Updating SQLite index...[/dim]")
        from scripts.build_index import get_db_path, create_schema, extract_index_fields, upsert_row
        import sqlite3

        db_path = get_db_path(config)
        conn = sqlite3.connect(db_path)
        create_schema(conn)
        fields = extract_index_fields(output_path)
        upsert_row(conn, fields)
        conn.close()
        result["steps_completed"].append("index")

        # Validation (optional)
        if validate:
            gold_path = ROOT_DIR / "data" / "gold_standard" / f"{study_id}_extraction.json"
            if gold_path.exists():
                console.print("  [dim]Validating against gold standard...[/dim]")
                from scripts.validate import validate_extraction, print_report
                with open(gold_path) as f:
                    gold = json.load(f)
                report = validate_extraction(paper, gold)
                print_report(report)
                result["validation_accuracy"] = report["overall_accuracy"]
            else:
                console.print(f"  [yellow]No gold standard found for {study_id}[/yellow]")

        # Cost summary
        llm.print_cost_summary()
        result["cost"] = llm.get_cost_summary()
        result["status"] = "success"
        result["end_time"] = datetime.now().isoformat()

        console.print(f"\n[bold green]✅ Extraction complete: {study_id}[/bold green]")
        console.print(f"  Output: {output_path}")
        console.print(f"  Experiments: {len(paper.get('experiments', []))}")
        console.print(f"  Data gaps: {paper.get('extraction_metadata', {}).get('num_data_gaps', 'N/A')}")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        result["end_time"] = datetime.now().isoformat()
        console.print(f"\n[bold red]✗ Pipeline failed: {e}[/bold red]")
        import traceback
        traceback.print_exc()

    return result


# ── Prompt Runners ───────────────────────────────────────────

def _run_prompt_a(llm, article, wee_example, benabu_example) -> dict:
    """Run Prompt A: Study Metadata."""
    prompt_template = _load_prompt_safe("prompt_a_metadata")
    prompt = prompt_template.format(
        gold_standard_wee=_extract_section(wee_example, "study_metadata"),
        gold_standard_benabu=_extract_section(benabu_example, "study_metadata"),
        article_text=article.full_text[:15000],
    )
    return llm.extract_json(prompt)


def _run_prompt_b(llm, article, study_id, wee_example, benabu_example) -> dict:
    """Run Prompt B: Experiment Design."""
    prompt_template = _load_prompt_safe("prompt_b_experiment")
    prompt = prompt_template.format(
        study_id=study_id,
        gold_standard_wee_experiment=_extract_section(wee_example, "experiments"),
        gold_standard_benabu_experiment=_extract_section(benabu_example, "experiments"),
        methods_text=article.get_methods_text()[:10000],
        article_text=article.full_text[:15000],
    )
    return llm.extract_json(prompt)


def _run_prompt_c(llm, article, study_id, wee_example, benabu_example) -> dict:
    """Run Prompt C: Stimuli."""
    prompt_template = _load_prompt_safe("prompt_c_stimuli")
    prompt = prompt_template.format(
        study_id=study_id,
        gold_standard_wee_stimuli=_extract_section(wee_example, "stimuli"),
        gold_standard_benabu_stimuli=_extract_section(benabu_example, "stimuli"),
        methods_text=article.get_methods_text()[:10000],
        results_text=article.get_results_text()[:10000],
        tables_markdown=article.get_tables_as_markdown()[:5000],
    )
    return llm.extract_json(prompt)


def _run_prompt_d(llm, article, study_id, wee_example, benabu_example) -> dict:
    """Run Prompt D: Sensory Data + Derived Metrics."""
    prompt_template = _load_prompt_safe("prompt_d_sensory_data")
    prompt = prompt_template.format(
        study_id=study_id,
        gold_standard_wee_data=_extract_section(wee_example, "sensory_data"),
        gold_standard_benabu_data=_extract_section(benabu_example, "sensory_data"),
        methods_text=article.get_methods_text()[:8000],
        results_text=article.get_results_text()[:12000],
        tables_markdown=article.get_tables_as_markdown()[:8000],
    )
    return llm.extract_json(prompt)


def _run_prompt_e(llm, figure_meta, article, study_id, wee_example) -> dict:
    """Run Prompt E: Figure Extraction (uses vision model)."""
    prompt_template = _load_prompt_safe("prompt_e_figures")

    # Get scale info from article
    scale_info = "Unknown"
    for section_text in article.sections.values():
        if "gLMS" in section_text:
            scale_info = "gLMS (0-100)"
            break
        elif "Likert" in section_text or "9-point" in section_text:
            scale_info = "9-point Likert (1-9)"
            break

    prompt = prompt_template.format(
        study_id=study_id,
        figure_caption=figure_meta.get("caption", ""),
        surrounding_text=figure_meta.get("surrounding_text", "")[:500],
        scale_info=scale_info,
        gold_standard_figure_example="See gold standard JSONs for expected figure data format.",
    )

    return llm.extract_json_with_image(prompt, figure_meta["local_path"])


# ── Helpers ──────────────────────────────────────────────────

def _load_prompt_safe(name: str) -> str:
    """Load a prompt template, returning a basic fallback if not found."""
    try:
        from scripts.llm_extract import load_prompt
        return load_prompt(name)
    except FileNotFoundError:
        console.print(f"  [yellow]⚠ Prompt template {name}.txt not found[/yellow]")
        return "Extract the requested data from the article below and return as JSON.\n\n{article_text}"


def _extract_section(json_str: str, section: str) -> str:
    """Extract a specific section from a gold-standard JSON string for few-shot use."""
    try:
        data = json.loads(json_str) if isinstance(json_str, str) else json_str
        if section in data:
            return json.dumps({section: data[section]}, indent=2)[:3000]
        # For experiments, extract first experiment's subsection
        for exp in data.get("experiments", []):
            if section in exp:
                return json.dumps(exp[section], indent=2)[:3000]
    except (json.JSONDecodeError, TypeError):
        pass
    return "{}"


def _save_part(path: Path, data: dict):
    """Save an extraction part to disk."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── CLI ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sensory Data Extraction Pipeline — Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--doi", type=str, help="DOI of a single paper to extract")
    parser.add_argument("--study-id", type=str, default="", help="Custom study ID (default: derived from DOI)")
    parser.add_argument("--doi-list", type=str, help="Path to CSV file with DOI column")
    parser.add_argument("--skip-figures", action="store_true", help="Skip figure download and extraction")
    parser.add_argument("--force", action="store_true", help="Re-extract even if output exists")
    parser.add_argument("--validate", action="store_true", help="Validate against gold standard after extraction")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without calling APIs")

    args = parser.parse_args()

    if not args.doi and not args.doi_list:
        parser.print_help()
        sys.exit(1)

    config = load_config()
    results = []

    if args.doi:
        result = run_pipeline(
            doi=args.doi,
            study_id=args.study_id,
            config=config,
            skip_figures=args.skip_figures,
            force=args.force,
            validate=args.validate,
            dry_run=args.dry_run,
        )
        results.append(result)

    elif args.doi_list:
        csv_path = Path(args.doi_list)
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            papers = list(reader)

        console.print(f"\n[bold]Processing {len(papers)} papers from {csv_path}[/bold]")

        for i, row in enumerate(papers, 1):
            doi = row.get("doi", row.get("DOI", ""))
            sid = row.get("study_id", row.get("id", ""))

            if not doi:
                console.print(f"  [yellow]Row {i}: no DOI found, skipping[/yellow]")
                continue

            console.print(f"\n{'─' * 60}")
            console.print(f"[bold]Paper {i}/{len(papers)}[/bold]")

            result = run_pipeline(
                doi=doi,
                study_id=sid,
                config=config,
                skip_figures=args.skip_figures,
                force=args.force,
                validate=args.validate,
                dry_run=args.dry_run,
            )
            results.append(result)

    # Summary
    _print_summary(results)


def _print_summary(results: list[dict]):
    """Print a summary table of all processed papers."""
    if len(results) <= 1:
        return

    console.print(f"\n{'═' * 60}")
    table = Table(title="Pipeline Summary")
    table.add_column("Study ID", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Steps")
    table.add_column("Errors")

    for r in results:
        status_style = "green" if r["status"] == "success" else "red"
        table.add_row(
            r.get("study_id", r["doi"]),
            f"[{status_style}]{r['status']}[/{status_style}]",
            str(len(r.get("steps_completed", []))),
            str(len(r.get("errors", []))),
        )

    console.print(table)

    success = sum(1 for r in results if r["status"] == "success")
    console.print(f"\n  {success}/{len(results)} papers extracted successfully")


if __name__ == "__main__":
    main()
