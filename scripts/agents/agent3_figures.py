#!/usr/bin/env python3
"""Agent 3 — Figure Extraction: Extract data from figures as flat observations."""

import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.llm_extract import LLMClient, PromptTooLargeError, load_prompt
from scripts.db import get_db, get_paper_observations, get_panels_for_paper, insert_observations_batch

console = Console()


def run_agent3(figure_metadata: list, agent1_output: dict, agent2_output: dict,
               paper_id: str, run_id: int, config: dict = None,
               llm: LLMClient = None) -> dict:
    """Run Agent 3: Extract data from figures using vision model.

    Args:
        figure_metadata: List of dicts with figure info (local_path, figure_id, caption)
        agent1_output: Agent 1's extraction JSON (for figure context)
        agent2_output: Agent 2's structured output (for existing observations and experiments)
        paper_id: Paper identifier
        run_id: Extraction run ID
        config: Config dict
        llm: LLMClient instance

    Returns:
        Dict with observations, observations_inserted, extraction_notes, db_insert_error
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
    existing_obs_summary = _build_existing_observations_summary(agent2_output)
    experiment_context = _build_experiment_context(agent2_output)
    experiment_context_str = json.dumps(experiment_context, indent=2)
    panel_context = _build_panel_context(agent2_output, paper_id, config)
    panel_context_str = json.dumps(panel_context, indent=2)

    all_observations = []
    all_notes = []

    # Track accumulated observations for cross-figure dedup
    accumulated_obs = list(existing_obs_summary)

    for fig in figure_metadata:
        local_path = fig.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            console.print(f"    [yellow]⚠ Skipping {fig.get('figure_id', '?')}: no local image[/yellow]")
            continue

        fig_id = fig.get("figure_id", "unknown")
        caption = fig.get("caption", "")

        # Get figure description from Agent 1 inventory
        fig_description = _get_figure_description(agent1_output, fig_id)

        # Build per-figure dedup summary (Agent 2 obs + prior figures)
        dedup_summary_str = json.dumps(accumulated_obs, indent=2)

        # Fill prompt
        prompt = prompt_template
        prompt = prompt.replace("{figure_caption}", caption)
        prompt = prompt.replace("{figure_description}", fig_description)
        prompt = prompt.replace("{existing_observations_summary}", dedup_summary_str)
        prompt = prompt.replace("{experiment_context}", experiment_context_str)
        prompt = prompt.replace("{panel_context}", panel_context_str)
        prompt = prompt.replace("{paper_id}", paper_id)

        try:
            result = llm.extract_json_with_image(prompt, local_path, model=model, agent="agent3")

            new_obs = result.get("new_observations", [])
            # Tag all observations with paper_id, run_id, source
            for obs in new_obs:
                obs["paper_id"] = paper_id
                obs["run_id"] = run_id
                obs["source_type"] = "figure"
                if not obs.get("source"):
                    obs["source"] = fig_id

            all_observations.extend(new_obs)
            if result.get("extraction_notes"):
                all_notes.append(f"{fig_id}: {result['extraction_notes']}")

            # Accumulate for cross-figure dedup
            for obs in new_obs:
                obs_comps = obs.get("components") or []
                obs_conc = obs_comps[0].get("concentration") if obs_comps else None
                accumulated_obs.append({
                    "substance": obs.get("substance"),
                    "concentration": obs_conc,
                    "attribute": obs.get("attribute_normalized", obs.get("attribute", "")),
                    "value": obs.get("value"),
                    "source": obs.get("source", fig_id),
                })

            console.print(f"    [green]✓ {fig_id}: {len(new_obs)} new data points[/green]")

        except PromptTooLargeError as e:
            console.print(f"    [yellow]⚠ {fig_id}: prompt too large ({e.prompt_chars:,} chars), skipping[/yellow]")
            all_notes.append(f"{fig_id}: skipped — {e}")
        except Exception as e:
            console.print(f"    [red]✗ {fig_id}: {e}[/red]")
            all_notes.append(f"{fig_id}: extraction failed — {e}")

    # Build output dict — always returned even if DB insert fails
    output = {
        "observations": all_observations,
        "observations_inserted": 0,
        "extraction_notes": all_notes,
        "db_insert_error": None,
    }

    if all_observations:
        try:
            conn = get_db(config)

            # Validate experiment references
            valid_exp_ids = {row["experiment_id"] for row in conn.execute(
                "SELECT experiment_id FROM experiments WHERE paper_id = ?",
                (paper_id,),
            ).fetchall()}

            # Build panel label → panel_id map for FK resolution
            panel_rows = conn.execute(
                "SELECT panel_id, panel_label, parent_panel_id FROM panels WHERE paper_id = ?",
                (paper_id,),
            ).fetchall()
            panel_label_to_id = {r["panel_label"]: r["panel_id"] for r in panel_rows}
            # Fallback map: for each experiment prefix, find the full (non-subgroup) panel
            _full_panels = {r["panel_label"].split("_")[0]: r["panel_id"]
                           for r in panel_rows if not r["parent_panel_id"]}

            db_rows = []
            dropped = []
            panel_mismatches = 0
            for obs in all_observations:
                exp_label = obs.get("experiment", "exp1")
                experiment_id = f"{paper_id}__exp{exp_label.replace('exp', '')}"

                if experiment_id not in valid_exp_ids:
                    dropped.append(
                        f"({obs.get('substance')}, {obs.get('attribute')}): "
                        f"experiment_id '{experiment_id}' not in DB"
                    )
                    continue

                # Resolve panel_id: exact match → fallback to experiment's full panel → NULL
                obs_panel_label = obs.get("panel_label")
                panel_id = panel_label_to_id.get(obs_panel_label) if obs_panel_label else None
                if obs_panel_label and panel_id is None:
                    # Fallback: try to find the full panel for this experiment
                    panel_id = _full_panels.get(exp_label)
                    if panel_id:
                        panel_mismatches += 1

                components = obs.get("components")
                if isinstance(components, list):
                    components_json = components
                else:
                    components_json = None

                db_rows.append({
                    "paper_id": paper_id,
                    "experiment_id": experiment_id,
                    "panel_id": panel_id,
                    "measurement_domain": obs.get("measurement_domain", "sensory"),
                    "substance_name": obs.get("substance"),
                    "components_json": components_json,
                    "base_matrix": obs.get("base_matrix"),
                    "is_control": obs.get("is_control", False),
                    "attribute_raw": obs.get("attribute"),
                    "attribute_normalized": obs.get("attribute_normalized"),
                    "value": obs.get("value"),
                    "value_type": obs.get("value_type"),
                    "error_value": obs.get("error"),
                    "error_type": obs.get("error_type"),
                    "source_type": obs.get("source_type", "figure"),
                    "source_location": obs.get("source"),
                    "extraction_confidence": obs.get("confidence"),
                    "run_id": run_id,
                })

            inserted = insert_observations_batch(conn, db_rows) if db_rows else 0
            conn.close()

            output["observations_inserted"] = inserted
            if dropped:
                output["dropped"] = dropped
                console.print(f"  [yellow]⚠ {len(dropped)} observations dropped (invalid experiment refs)[/yellow]")
            if panel_mismatches:
                console.print(f"  [yellow]⚠ {panel_mismatches} observations had unrecognized panel_label — fell back to experiment's full panel[/yellow]")
            console.print(f"  [green]✓ Agent 3 complete: {inserted} figure data points inserted[/green]")

        except Exception as e:
            output["db_insert_error"] = str(e)
            console.print(f"  [yellow]⚠ Agent 3 DB insert failed: {e}[/yellow]")
            console.print(f"  [dim]Observations preserved in output dict for artifact save.[/dim]")
    else:
        console.print(f"  [green]✓ Agent 3 complete: no figure data points extracted[/green]")

    return output


def _build_existing_observations_summary(agent2_output: dict) -> list[dict]:
    """Build compact dedup summary from Agent 2's observations."""
    observations = agent2_output.get("observations", [])
    summaries = []
    for obs in observations:
        components = obs.get("components") or []
        concentration = components[0].get("concentration") if components else None
        summaries.append({
            "substance": obs.get("substance"),
            "concentration": concentration,
            "attribute": obs.get("attribute_normalized", obs.get("attribute", "")),
            "value": obs.get("value"),
            "source": obs.get("source", ""),
        })
    return summaries


def _build_experiment_context(agent2_output: dict) -> list[dict]:
    """Build experiment context (method, scale) for Agent 3."""
    experiments = agent2_output.get("experiments", [])
    return [
        {
            "experiment": e.get("experiment"),
            "method": e.get("method"),
            "scale_type": e.get("scale_type"),
            "scale_range": e.get("scale_range"),
        }
        for e in experiments
    ]


def _build_panel_context(agent2_output: dict, paper_id: str, config: dict) -> list[dict]:
    """Build panel context (label, experiment, size) for Agent 3.

    Prefers panels from Agent 2's output; falls back to DB query if panels are absent.
    """
    panels = agent2_output.get("panels", [])
    if panels:
        return [
            {
                "panel_label": p.get("panel_label"),
                "experiment": p.get("experiment"),
                "panel_size": p.get("panel_size"),
                "parent_panel_label": p.get("parent_panel_label"),
            }
            for p in panels
        ]
    # Fallback: read from DB (e.g., when resuming from Agent 3)
    try:
        conn = get_db(config)
        rows = conn.execute(
            "SELECT panel_label, panel_size, parent_panel_id FROM panels WHERE paper_id = ?",
            (paper_id,),
        ).fetchall()
        conn.close()
        return [
            {
                "panel_label": r["panel_label"],
                "panel_size": r["panel_size"],
                "parent_panel_label": None,  # simplified fallback
            }
            for r in rows
        ]
    except Exception:
        return []


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
