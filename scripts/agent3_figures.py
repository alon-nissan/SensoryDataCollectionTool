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
from scripts.db import get_db, get_paper_results, insert_results_batch

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

    all_results = []
    all_unmatched = []
    all_notes = []

    for fig in figure_metadata:
        local_path = fig.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            console.print(f"    [yellow]⚠ Skipping {fig.get('figure_id', '?')}: no local image[/yellow]")
            continue

        fig_id = fig.get("figure_id", "unknown")
        caption = fig.get("caption", "")

        # Get figure description from Agent 1 inventory
        fig_description = _get_figure_description(agent1_output, fig_id)

        # Fill prompt
        prompt = prompt_template
        prompt = prompt.replace("{figure_caption}", caption)
        prompt = prompt.replace("{figure_description}", fig_description)
        prompt = prompt.replace("{existing_sample_ids}", json.dumps(sample_ids, indent=2)[:3000])
        prompt = prompt.replace("{existing_results_summary}", json.dumps(existing_summary, indent=2)[:5000])
        prompt = prompt.replace("{experiment_context}", json.dumps(experiment_context, indent=2)[:2000])
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

            console.print(f"    [green]✓ {fig_id}: {len(new_results)} new data points[/green]")

        except Exception as e:
            console.print(f"    [red]✗ {fig_id}: {e}[/red]")
            all_notes.append(f"{fig_id}: extraction failed — {e}")

    # Commit to database
    conn = get_db(config)
    inserted = insert_results_batch(conn, all_results) if all_results else 0
    conn.close()

    console.print(f"  [green]✓ Agent 3 complete: {inserted} figure data points inserted[/green]")

    return {
        "results": all_results,
        "results_inserted": inserted,
        "unmatched_samples": all_unmatched,
        "extraction_notes": all_notes,
    }


def _build_sample_id_list(agent2_output: dict) -> list[dict]:
    """Build a list of sample IDs with labels for Agent 3's context."""
    samples = agent2_output.get("samples", [])
    return [{"sample_id": s.get("sample_id"), "label": s.get("sample_label", "")}
            for s in samples]


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
