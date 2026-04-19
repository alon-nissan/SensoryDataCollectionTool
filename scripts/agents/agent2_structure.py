#!/usr/bin/env python3
"""Agent 2 — Structuring: Convert Agent 1's flexible JSON into flat observations."""

import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.llm_extract import LLMClient, load_prompt
from scripts.db.db import (
    get_db, insert_paper, insert_experiment, insert_panel,
    insert_substance, add_substance_alias, insert_observations_batch,
    resolve_substance_by_alias, resolve_substance_by_name,
    resolve_substance_by_cas,
)

console = Console()


def run_agent2(agent1_output: dict, paper_id: str, config: dict = None,
               llm: LLMClient = None) -> dict:
    """Run Agent 2: Structure Agent 1's JSON into flat observations.

    Args:
        agent1_output: Agent 1's extraction JSON
        paper_id: DOI-derived paper identifier
        config: Config dict
        llm: LLMClient instance

    Returns:
        Structured output dict with observations, experiments, context
    """
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    if llm is None:
        llm = LLMClient(config)

    console.print("  [dim]Agent 2: Structuring into observations...[/dim]")

    # Load prompt template
    prompt_template = load_prompt("agent2_structuring")

    # Load attribute vocabulary
    vocab_path = ROOT_DIR / config["paths"]["vocabulary_file"]
    attribute_vocab = {}
    if vocab_path.exists():
        with open(vocab_path) as f:
            vocab_data = json.load(f)
            attribute_vocab = vocab_data.get("mappings", {})

    agent1_json = json.dumps(agent1_output, indent=2)

    prompt = prompt_template
    prompt = prompt.replace("{agent1_json}", agent1_json)
    prompt = prompt.replace("{attribute_vocabulary}", json.dumps(attribute_vocab, indent=2))
    prompt = prompt.replace("{paper_id}", paper_id)

    # Call LLM
    model = llm.get_model("agent2")
    result = llm.extract_json(prompt, model=model, agent="agent2")

    n_obs = len(result.get("observations", []))
    n_exp = len(result.get("experiments", []))
    n_panels = len(result.get("panels", []))
    n_derived = sum(1 for o in result.get("observations", [])
                    if o.get("value_type") == "derived_param")
    console.print(f"  [green]✓ Agent 2 complete: "
                  f"{n_exp} experiments, "
                  f"{n_panels} panels, "
                  f"{n_obs} observations ({n_derived} derived metrics)[/green]")

    return result


def commit_agent2_to_db(structured: dict, paper_id: str, run_id: int,
                        config: dict = None) -> dict:
    """Insert Agent 2's structured output into the SQLite database.

    Args:
        structured: Agent 2's output dict
        paper_id: Paper identifier
        run_id: Extraction run ID
        config: Config dict

    Returns:
        Dict with counts, dropped entities, and any db_insert_error
    """
    conn = get_db(config)
    output = {
        "counts": {"papers": 0, "experiments": 0, "panels": 0, "observations": 0},
        "dropped": [],
        "db_insert_error": None,
    }
    counts = output["counts"]

    try:
        # 1. Insert paper (minimal)
        paper_data = structured.get("paper", {})
        paper_data["paper_id"] = paper_id
        insert_paper(conn, paper_data)
        counts["papers"] = 1

        # 2. Insert experiments
        for exp in structured.get("experiments", []):
            exp_label = exp.get("experiment", "exp1")
            exp_id = f"{paper_id}__exp{exp_label.replace('exp', '')}"
            try:
                insert_experiment(conn, {
                    "experiment_id": exp_id,
                    "paper_id": paper_id,
                    "experiment_label": exp.get("label"),
                    "sensory_method": exp.get("method"),
                    "scale_type": exp.get("scale_type"),
                    "scale_range": exp.get("scale_range"),
                })
                counts["experiments"] += 1
            except Exception as e:
                reason = f"experiment '{exp_id}': {e}"
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")

        # 3. Insert panels (measuring devices)
        # Build label → panel_id map for observation FK assignment
        panel_label_to_id: dict[str, str] = {}
        # First pass: insert full panels (parent_panel_label == null) before subgroups
        panels_raw = structured.get("panels", [])
        for panel in sorted(panels_raw, key=lambda p: 0 if not p.get("parent_panel_label") else 1):
            panel_label = panel.get("panel_label", "unknown")
            panel_id = f"{paper_id}__panel_{panel_label}"

            parent_label = panel.get("parent_panel_label")
            parent_panel_id = panel_label_to_id.get(parent_label) if parent_label else None

            try:
                insert_panel(conn, {
                    "panel_id": panel_id,
                    "paper_id": paper_id,
                    "parent_panel_id": parent_panel_id,
                    "panel_label": panel_label,
                    "panel_size": panel.get("panel_size"),
                    "attributes_json": panel.get("attributes_json"),
                    "description": panel.get("description"),
                })
                panel_label_to_id[panel_label] = panel_id
                counts["panels"] += 1
            except Exception as e:
                reason = f"panel '{panel_id}': {e}"
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")

        # Build fallback map: for each experiment prefix, the full (non-subgroup) panel
        _full_panels: dict[str, str] = {}
        for p in panels_raw:
            if not p.get("parent_panel_label"):
                exp = p.get("experiment", "")
                label = p.get("panel_label", "")
                if exp and label in panel_label_to_id:
                    _full_panels[exp] = panel_label_to_id[label]

        # 4. Materialize observations (flat loop — no FK chains)
        valid_exp_ids = {row["experiment_id"] for row in conn.execute(
            "SELECT experiment_id FROM experiments WHERE paper_id = ?",
            (paper_id,),
        ).fetchall()}

        panel_mismatches = 0
        observations = []
        for obs in structured.get("observations", []):
            # Build experiment_id from label
            exp_label = obs.get("experiment", "exp1")
            experiment_id = f"{paper_id}__exp{exp_label.replace('exp', '')}"

            # Validate experiment exists
            if experiment_id not in valid_exp_ids:
                output["dropped"].append(
                    f"observation ({obs.get('substance')}, {obs.get('attribute')}): "
                    f"experiment_id '{experiment_id}' not in DB"
                )
                continue

            # Resolve panel_id: exact match → fallback to experiment's full panel → NULL
            obs_panel_label = obs.get("panel_label")
            panel_id = panel_label_to_id.get(obs_panel_label) if obs_panel_label else None
            if obs_panel_label and panel_id is None:
                panel_id = _full_panels.get(exp_label)
                if panel_id:
                    panel_mismatches += 1

            # Build components_json
            components = obs.get("components")
            if isinstance(components, list):
                components_json = components
            else:
                components_json = None

            observations.append({
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
                "source_type": obs.get("source_type"),
                "source_location": obs.get("source"),
                "extraction_confidence": obs.get("confidence"),
                "run_id": run_id,
            })

        if observations:
            counts["observations"] = insert_observations_batch(conn, observations)

        n_dropped = len(structured.get("observations", [])) - len(observations)
        if n_dropped > 0:
            console.print(
                f"  [yellow]⚠ {n_dropped} observations dropped "
                f"(invalid experiment refs)[/yellow]"
            )
        if panel_mismatches:
            console.print(
                f"  [yellow]⚠ {panel_mismatches} observations had unrecognized "
                f"panel_label — fell back to experiment's full panel[/yellow]"
            )
        console.print(f"  [dim]Inserted {counts['panels']} panels[/dim]")

        # 4. Save peripheral context to papers.context_json
        context = structured.get("context", {})
        if context:
            conn.execute(
                "UPDATE papers SET context_json = ? WHERE paper_id = ?",
                (json.dumps(context, ensure_ascii=False), paper_id),
            )
            conn.commit()
            console.print(f"  [dim]Saved peripheral context → papers.context_json[/dim]")

        # 5. Resolve substances (deterministic, post-extraction)
        _ensure_substance_registry(conn, observations, context)

    except Exception as e:
        output["db_insert_error"] = str(e)
        console.print(f"  [yellow]⚠ Agent 2 DB insert failed: {e}[/yellow]")
        console.print(f"  [dim]Partial data may have been committed.[/dim]")
    finally:
        conn.close()

    return output


def _ensure_substance_registry(conn, observations: list[dict], context: dict):
    """Populate the substance registry from observation data.

    Deterministic Python code — no LLM calls. Runs after observations
    are committed. Resolves substance names against the existing registry
    and creates new entries as needed.
    """
    # Collect unique substance names from observations
    substance_names = {
        obs["substance_name"]
        for obs in observations
        if obs.get("substance_name")
    }

    # Get sourcing info from peripheral context
    sourcing = context.get("substance_sourcing", {})

    for name in substance_names:
        name_lower = name.lower().strip()

        # Try existing resolution
        existing_id = (
            resolve_substance_by_alias(conn, name_lower) or
            resolve_substance_by_name(conn, name_lower)
        )
        if existing_id:
            continue

        # Also check with CAS from sourcing info
        details = sourcing.get(name, {})
        cas = details.get("cas_number")
        if cas:
            existing_id = resolve_substance_by_cas(conn, cas)
            if existing_id:
                # Link this name as an alias
                add_substance_alias(conn, name_lower, existing_id)
                continue

        # Create new substance
        try:
            new_id = insert_substance(conn, {
                "normalized_name": name_lower,
                "cas_number": cas,
                "category": details.get("category"),
            })
            add_substance_alias(conn, name_lower, new_id)
        except Exception:
            # UNIQUE constraint — already exists via another path
            pass


def save_agent2_output(result: dict, study_id: str, config: dict = None) -> Path:
    """Save Agent 2 output for debugging/audit."""
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    parts_dir = ROOT_DIR / config["paths"]["extractions_dir"] / "parts" / study_id
    parts_dir.mkdir(parents=True, exist_ok=True)

    output_path = parts_dir / "agent2_structured.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return output_path
