#!/usr/bin/env python3
"""Agent 2 — Structuring: Convert Agent 1's flexible JSON into SQLite table rows."""

import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.llm_extract import LLMClient, load_prompt
from scripts.db import (
    get_db, insert_paper, insert_experiment, insert_substance,
    add_substance_alias, insert_stimulus, insert_sample,
    insert_sample_component, insert_result, insert_results_batch,
    resolve_substance_by_alias, resolve_substance_by_name,
    resolve_substance_by_cas, get_all_substance_aliases,
)
from scripts.paper_id import doi_to_paper_id

console = Console()


def run_agent2(agent1_output: dict, paper_id: str, config: dict = None,
               llm: LLMClient = None) -> dict:
    """Run Agent 2: Structure Agent 1's JSON into database rows.

    Args:
        agent1_output: Agent 1's extraction JSON
        paper_id: DOI-derived paper identifier
        config: Config dict
        llm: LLMClient instance

    Returns:
        Structured output dict with rows for each table
    """
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    if llm is None:
        llm = LLMClient(config)

    console.print("  [dim]Agent 2: Structuring into database rows...[/dim]")

    # Load prompt template
    prompt_template = load_prompt("agent2_structuring")

    # Get substance aliases for deterministic lookup
    conn = get_db(config)
    aliases = get_all_substance_aliases(conn)
    conn.close()

    # Load attribute vocabulary
    vocab_path = ROOT_DIR / config["paths"]["vocabulary_file"]
    attribute_vocab = {}
    if vocab_path.exists():
        with open(vocab_path) as f:
            vocab_data = json.load(f)
            attribute_vocab = vocab_data.get("mappings", {})

    # Fill template
    prompt = prompt_template
    prompt = prompt.replace("{agent1_json}", json.dumps(agent1_output, indent=2)[:20000])
    prompt = prompt.replace("{substance_aliases}", json.dumps(aliases, indent=2)[:3000])
    prompt = prompt.replace("{attribute_vocabulary}", json.dumps(attribute_vocab, indent=2)[:3000])
    prompt = prompt.replace("{paper_id}", paper_id)

    # Call LLM
    model = llm.get_model("agent2")
    result = llm.extract_json(prompt, model=model)

    console.print(f"  [green]✓ Agent 2 complete: "
                  f"{len(result.get('experiments', []))} experiments, "
                  f"{len(result.get('results', []))} results, "
                  f"{len(result.get('samples', []))} samples[/green]")

    return result


def commit_agent2_to_db(structured: dict, paper_id: str, run_id: int,
                        config: dict = None) -> dict:
    """Insert Agent 2's structured output into the SQLite database.

    Resilient to FK constraint failures — individual entities that fail are
    logged and skipped rather than crashing the pipeline.

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
        "counts": {"papers": 0, "experiments": 0, "substances": 0, "stimuli": 0,
                   "samples": 0, "sample_components": 0, "results": 0},
        "dropped": [],
        "db_insert_error": None,
    }
    counts = output["counts"]

    try:
        # 1. Insert paper
        paper_data = structured.get("paper", {})
        paper_data["paper_id"] = paper_id
        if structured.get("context_json"):
            paper_data["context_json"] = structured["context_json"]
        insert_paper(conn, paper_data)
        counts["papers"] = 1

        # 2. Insert experiments
        for exp in structured.get("experiments", []):
            exp["paper_id"] = paper_id
            try:
                insert_experiment(conn, exp)
                counts["experiments"] += 1
            except Exception as e:
                reason = f"experiment '{exp.get('experiment_id')}': {e}"
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")

        # 3. Resolve and insert substances
        substance_id_map = {}  # Maps LLM-provided name → actual substance_id
        for sub in structured.get("substances", []):
            name = sub.get("normalized_name", "").lower().strip()
            if not name:
                continue

            # Try existing resolution first
            existing_id = (
                resolve_substance_by_alias(conn, name) or
                resolve_substance_by_name(conn, name) or
                resolve_substance_by_cas(conn, sub.get("cas_number"))
            )

            if existing_id:
                substance_id_map[name] = existing_id
            else:
                try:
                    new_id = insert_substance(conn, sub)
                    substance_id_map[name] = new_id
                    add_substance_alias(conn, name, new_id)
                    counts["substances"] += 1
                except Exception as e:
                    reason = f"substance '{name}': {e}"
                    output["dropped"].append(reason)
                    console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")

        # 4. Insert stimuli (resolve substance_id via helper)
        for stim in structured.get("stimuli", []):
            stim["paper_id"] = paper_id
            stim["substance_id"] = _ensure_substance_for_stimulus(
                conn, stim, substance_id_map
            )
            if stim["substance_id"] is None:
                reason = (f"stimulus '{stim.get('stimulus_id')}': "
                          "no resolvable substance")
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")
                continue
            try:
                insert_stimulus(conn, stim)
                counts["stimuli"] += 1
            except Exception as e:
                reason = f"stimulus '{stim.get('stimulus_id')}': {e}"
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")

        # 5. Insert samples (pre-validate experiment_id FK)
        valid_exp_ids = {row["experiment_id"] for row in conn.execute(
            "SELECT experiment_id FROM experiments WHERE paper_id = ?",
            (paper_id,),
        ).fetchall()}
        for sample in structured.get("samples", []):
            sample["paper_id"] = paper_id
            exp_id = sample.get("experiment_id")
            if exp_id and exp_id not in valid_exp_ids:
                reason = (f"sample '{sample.get('sample_id')}': "
                          f"experiment_id '{exp_id}' not in DB")
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")
                continue
            try:
                insert_sample(conn, sample)
                counts["samples"] += 1
            except Exception as e:
                reason = f"sample '{sample.get('sample_id')}': {e}"
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")

        # 6. Insert sample_components (pre-validate FKs)
        valid_sample_ids = {row["sample_id"] for row in conn.execute(
            "SELECT sample_id FROM samples WHERE paper_id = ?",
            (paper_id,),
        ).fetchall()}
        valid_stimulus_ids = {row["stimulus_id"] for row in conn.execute(
            "SELECT stimulus_id FROM stimuli WHERE paper_id = ?",
            (paper_id,),
        ).fetchall()}
        for comp in structured.get("sample_components", []):
            sid = comp.get("sample_id")
            stim_id = comp.get("stimulus_id")
            reasons = []
            if sid and sid not in valid_sample_ids:
                reasons.append(f"sample_id '{sid}' not in DB")
            if stim_id and stim_id not in valid_stimulus_ids:
                reasons.append(f"stimulus_id '{stim_id}' not in DB")
            if reasons:
                reason = f"sample_component ({sid}, {stim_id}): {', '.join(reasons)}"
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")
                continue
            try:
                insert_sample_component(conn, comp)
                counts["sample_components"] += 1
            except Exception as e:
                reason = f"sample_component ({sid}, {stim_id}): {e}"
                output["dropped"].append(reason)
                console.print(f"  [yellow]⚠ Skipped {reason}[/yellow]")

        # 7. Insert results (filter to valid FK refs, then batch insert)
        results = structured.get("results", [])
        for r in results:
            r["paper_id"] = paper_id
            r["run_id"] = run_id
        valid_results = []
        for r in results:
            exp_id = r.get("experiment_id")
            sample_id = r.get("sample_id")
            reasons = []
            if exp_id and exp_id not in valid_exp_ids:
                reasons.append(f"experiment_id '{exp_id}' not in DB")
            if sample_id and sample_id not in valid_sample_ids:
                reasons.append(f"sample_id '{sample_id}' not in DB")
            if reasons:
                output["dropped"].append(
                    f"result ({exp_id}, {sample_id}): {', '.join(reasons)}"
                )
            else:
                valid_results.append(r)
        if len(valid_results) < len(results):
            n_dropped = len(results) - len(valid_results)
            console.print(
                f"  [yellow]⚠ {n_dropped} results dropped (invalid FK refs)[/yellow]"
            )
        counts["results"] = insert_results_batch(conn, valid_results)

    except Exception as e:
        output["db_insert_error"] = str(e)
        console.print(f"  [yellow]⚠ Agent 2 DB insert failed: {e}[/yellow]")
        console.print(f"  [dim]Partial data may have been committed.[/dim]")
    finally:
        conn.close()

    return output


def _ensure_substance_for_stimulus(conn, stim: dict,
                                   substance_id_map: dict) -> int | None:
    """Resolve or auto-create a substance for a stimulus dict.

    Returns a valid substance_id, or None if no name is available.
    """
    # If LLM provided a substance_id, validate it actually exists in DB
    llm_id = stim.get("substance_id")
    if llm_id and isinstance(llm_id, int):
        row = conn.execute(
            "SELECT substance_id FROM substances WHERE substance_id = ?",
            (llm_id,),
        ).fetchone()
        if row:
            return llm_id

    # Collect candidate names
    candidates = []
    for key in ("substance_name", "normalized_name", "original_name"):
        val = stim.get(key, "")
        if val and isinstance(val, str) and val.strip():
            candidates.append(val.strip())
    if not candidates:
        return None

    # Try resolution with each candidate
    for name in candidates:
        resolved = (
            substance_id_map.get(name.lower().strip()) or
            resolve_substance_by_alias(conn, name) or
            resolve_substance_by_name(conn, name)
        )
        if resolved:
            substance_id_map[name.lower().strip()] = resolved
            return resolved

    # Auto-create a stub substance
    primary_name = candidates[0]
    try:
        new_id = insert_substance(conn, {"normalized_name": primary_name.lower()})
    except Exception:
        # UNIQUE constraint — another stimulus already created it
        resolved = resolve_substance_by_name(conn, primary_name)
        if resolved:
            substance_id_map[primary_name.lower().strip()] = resolved
            return resolved
        return None
    add_substance_alias(conn, primary_name, new_id)
    substance_id_map[primary_name.lower().strip()] = new_id
    console.print(
        f"    [cyan]Auto-created substance stub: '{primary_name}' → id {new_id}[/cyan]"
    )
    return new_id


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
