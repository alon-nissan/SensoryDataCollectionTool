#!/usr/bin/env python3
"""Agent 3 — Figure Extraction: Extract data from figures using Agent 2's sample IDs."""

import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.llm_extract import LLMClient, load_prompt
from scripts.db import get_db, get_paper_results, insert_results_batch, insert_experiment, insert_sample

console = Console()


def run_agent3(figure_metadata: list, agent1_output: dict, agent2_output: dict,
               paper_id: str, run_id: int, config: dict = None,
               llm: LLMClient = None) -> dict:
    """Run Agent 3: Extract data from figures using vision model.

    Args:
        figure_metadata: List of dicts with figure info (local_path, figure_id, caption)
        agent1_output: Agent 1's extraction JSON (for figure context)
        agent2_output: Agent 2's structured output (for sample IDs and existing results)
        paper_id: Paper identifier
        run_id: Extraction run ID
        config: Config dict
        llm: LLMClient instance

    Returns:
        Dict with all_results, unmatched_samples, extraction_notes
    """
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    if llm is None:
        llm = LLMClient(config)

    console.print(f"  [dim]Agent 3: Figure extraction ({len(figure_metadata)} figures)...[/dim]")

    prompt_template = load_prompt("agent3_figures")
    model = llm.get_model("agent3")

    # Build context from Agent 2's output
    sample_ids = _build_sample_id_list(agent2_output)
    existing_summary = _build_existing_results_summary(agent2_output)
    experiment_context = _build_experiment_context(agent2_output)

    # Use compact format for sample list to avoid truncation
    sample_ids_str = _format_sample_list_compact(sample_ids)
    experiment_context_str = json.dumps(experiment_context, indent=2)

    all_results = []
    all_unmatched = []
    all_notes = []

    # Track accumulated results across figures for deduplication (FIX 5)
    accumulated_results = list(existing_summary)  # Start with Agent 2's results

    for fig in figure_metadata:
        local_path = fig.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            console.print(f"    [yellow]⚠ Skipping {fig.get('figure_id', '?')}: no local image[/yellow]")
            continue

        fig_id = fig.get("figure_id", "unknown")
        caption = fig.get("caption", "")

        # Get figure description from Agent 1 inventory
        fig_description = _get_figure_description(agent1_output, fig_id)

        # Build per-figure dedup summary (Agent 2 results + results from prior figures)
        dedup_summary_str = json.dumps(accumulated_results, indent=2)

        # Fill prompt
        prompt = prompt_template
        prompt = prompt.replace("{figure_caption}", caption)
        prompt = prompt.replace("{figure_description}", fig_description)
        prompt = prompt.replace("{existing_sample_ids}", sample_ids_str)
        prompt = prompt.replace("{existing_results_summary}", dedup_summary_str)
        prompt = prompt.replace("{experiment_context}", experiment_context_str)
        prompt = prompt.replace("{paper_id}", paper_id)

        try:
            result = llm.extract_json_with_image(prompt, local_path, model=model)

            new_results = result.get("new_results", [])
            # Tag all results with paper_id and run_id
            for r in new_results:
                r["paper_id"] = paper_id
                r["run_id"] = run_id
                r["source_type"] = "figure"
                if not r.get("source_location"):
                    r["source_location"] = fig_id

            all_results.extend(new_results)
            all_unmatched.extend(result.get("unmatched_samples", []))
            if result.get("extraction_notes"):
                all_notes.append(f"{fig_id}: {result['extraction_notes']}")

            # Accumulate results for cross-figure deduplication
            for r in new_results:
                accumulated_results.append({
                    "sample_id": r.get("sample_id"),
                    "attribute": r.get("attribute_normalized", r.get("attribute_raw", "")),
                    "value": r.get("value"),
                    "source": r.get("source_location", fig_id),
                })

            console.print(f"    [green]✓ {fig_id}: {len(new_results)} new data points[/green]")

        except Exception as e:
            console.print(f"    [red]✗ {fig_id}: {e}[/red]")
            all_notes.append(f"{fig_id}: extraction failed — {e}")

    # Build output dict first — always returned, even if DB insert fails
    output = {
        "results": all_results,
        "results_inserted": 0,
        "unmatched_samples": all_unmatched,
        "extraction_notes": all_notes,
        "db_insert_error": None,
    }

    if all_results:
        try:
            conn = get_db(config)
            # Create stub rows for any missing FK references
            _ensure_referenced_entities(conn, all_results, paper_id, run_id, config)
            # Filter out any remaining invalid references (safety net)
            valid_results, dropped = _filter_valid_fk_references(conn, all_results, paper_id)
            if dropped:
                output["dropped_results"] = [
                    {"sample_id": d["result"].get("sample_id"),
                     "experiment_id": d["result"].get("experiment_id"),
                     "reasons": d["reasons"]}
                    for d in dropped
                ]
            # Insert only valid results
            inserted = insert_results_batch(conn, valid_results) if valid_results else 0
            conn.close()
            output["results_inserted"] = inserted
            console.print(f"  [green]✓ Agent 3 complete: {inserted} figure data points inserted[/green]")
            if dropped:
                console.print(f"  [yellow]⚠ {len(dropped)} results dropped (FK mismatch after stub creation)[/yellow]")
        except Exception as e:
            output["db_insert_error"] = str(e)
            console.print(f"  [yellow]⚠ Agent 3 DB insert failed: {e}[/yellow]")
            console.print(f"  [dim]Results preserved in output dict for artifact save.[/dim]")
    else:
        console.print(f"  [green]✓ Agent 3 complete: no figure data points extracted[/green]")

    return output


def _build_sample_id_list(agent2_output: dict) -> list[dict]:
    """Build a list of sample IDs with labels for Agent 3's context."""
    samples = agent2_output.get("samples", [])
    return [{"sample_id": s.get("sample_id"), "label": s.get("sample_label", "")}
            for s in samples]


def _format_sample_list_compact(sample_ids: list[dict]) -> str:
    """Format sample list in compact pipe-delimited format to avoid truncation.

    Instead of verbose JSON with indentation, produce one line per sample:
        sample_id | label
    This fits ~5x more samples in the same character budget.
    """
    lines = ["sample_id | label"]
    for s in sample_ids:
        sid = s.get("sample_id", "")
        label = s.get("label", "")
        lines.append(f"{sid} | {label}")
    return "\n".join(lines)


def _build_existing_results_summary(agent2_output: dict) -> list[dict]:
    """Build a compact summary of Agent 2's results for deduplication."""
    results = agent2_output.get("results", [])
    return [
        {
            "sample_id": r.get("sample_id"),
            "attribute": r.get("attribute_normalized", r.get("attribute_raw", "")),
            "value": r.get("value"),
            "source": r.get("source_location", ""),
        }
        for r in results[:200]  # Limit to avoid context overflow
    ]


def _build_experiment_context(agent2_output: dict) -> list[dict]:
    """Build experiment context (method, scale) for Agent 3."""
    experiments = agent2_output.get("experiments", [])
    return [
        {
            "experiment_id": e.get("experiment_id"),
            "method": e.get("sensory_method"),
            "scale_type": e.get("scale_type"),
            "scale_range": e.get("scale_range"),
        }
        for e in experiments
    ]


def _get_figure_description(agent1_output: dict, figure_id: str) -> str:
    """Get figure description from Agent 1's figure inventory."""
    for fig in agent1_output.get("figure_inventory", []):
        if fig.get("figure_id") == figure_id:
            return fig.get("description", "")
    return ""


def _ensure_referenced_entities(conn, all_results: list[dict],
                                paper_id: str, run_id: int, config: dict):
    """Create stub DB rows for experiment/sample IDs referenced by Agent 3
    results that don't yet exist in the database."""
    existing_exp_ids = {row["experiment_id"] for row in conn.execute(
        "SELECT experiment_id FROM experiments WHERE paper_id = ?", (paper_id,)
    ).fetchall()}
    existing_sample_ids = {row["sample_id"] for row in conn.execute(
        "SELECT sample_id FROM samples WHERE paper_id = ?", (paper_id,)
    ).fetchall()}

    referenced_exp_ids = {r["experiment_id"] for r in all_results if r.get("experiment_id")}
    referenced_sample_ids = {r["sample_id"] for r in all_results if r.get("sample_id")}

    # Create missing experiment stubs
    missing_exps = referenced_exp_ids - existing_exp_ids
    for exp_id in sorted(missing_exps):
        insert_experiment(conn, {
            "experiment_id": exp_id,
            "paper_id": paper_id,
            "experiment_label": "[figure-created]",
        })

    # Create missing sample stubs
    missing_samples = referenced_sample_ids - existing_sample_ids
    for sample_id in sorted(missing_samples):
        # Find first result referencing this sample for context
        ref_result = next((r for r in all_results if r.get("sample_id") == sample_id), None)
        exp_id = ref_result.get("experiment_id") if ref_result else None

        # Try to build a label from context_json
        label = "[figure-created]"
        if ref_result:
            ctx = ref_result.get("context_json")
            if isinstance(ctx, dict):
                label = ctx.get("sample_label", ctx.get("description", label))
            elif isinstance(ctx, str):
                try:
                    ctx_parsed = json.loads(ctx)
                    label = ctx_parsed.get("sample_label", ctx_parsed.get("description", label))
                except (json.JSONDecodeError, AttributeError):
                    pass

        # Ensure experiment_id exists (may need a stub too)
        if exp_id and exp_id not in existing_exp_ids and exp_id not in missing_exps:
            insert_experiment(conn, {
                "experiment_id": exp_id,
                "paper_id": paper_id,
                "experiment_label": "[figure-created]",
            })
            missing_exps.add(exp_id)

        insert_sample(conn, {
            "sample_id": sample_id,
            "paper_id": paper_id,
            "experiment_id": exp_id,
            "sample_label": label,
            "base_matrix": None,
            "is_control": 0,
        })

    if missing_exps or missing_samples:
        console.print(
            f"    [cyan]Created {len(missing_exps)} experiment + "
            f"{len(missing_samples)} sample stubs for figure-created entities[/cyan]"
        )


def _filter_valid_fk_references(conn, results: list[dict],
                                paper_id: str) -> tuple[list[dict], list[dict]]:
    """Filter results to only those with valid FK references.

    Returns (valid, dropped).
    """
    valid_exp_ids = {row["experiment_id"] for row in conn.execute(
        "SELECT experiment_id FROM experiments WHERE paper_id = ?", (paper_id,)
    ).fetchall()}
    valid_sample_ids = {row["sample_id"] for row in conn.execute(
        "SELECT sample_id FROM samples WHERE paper_id = ?", (paper_id,)
    ).fetchall()}

    valid, dropped = [], []
    for r in results:
        exp_id = r.get("experiment_id")
        sample_id = r.get("sample_id")
        reasons = []
        if exp_id and exp_id not in valid_exp_ids:
            reasons.append(f"experiment_id '{exp_id}' not found")
        if sample_id and sample_id not in valid_sample_ids:
            reasons.append(f"sample_id '{sample_id}' not found")
        if reasons:
            dropped.append({"result": r, "reasons": reasons})
        else:
            valid.append(r)

    if dropped:
        for d in dropped:
            console.print(f"    [dim]↳ Dropped: {', '.join(d['reasons'])}[/dim]")

    return valid, dropped


def save_agent3_output(result: dict, study_id: str, config: dict = None) -> Path:
    """Save Agent 3 output for audit."""
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    parts_dir = ROOT_DIR / config["paths"]["extractions_dir"] / "parts" / study_id
    parts_dir.mkdir(parents=True, exist_ok=True)

    output_path = parts_dir / "agent3_figures.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return output_path
