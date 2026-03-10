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

    Args:
        structured: Agent 2's output dict
        paper_id: Paper identifier
        run_id: Extraction run ID
        config: Config dict

    Returns:
        Summary dict with counts of inserted rows per table
    """
    conn = get_db(config)
    counts = {"papers": 0, "experiments": 0, "substances": 0, "stimuli": 0,
              "samples": 0, "sample_components": 0, "results": 0}

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
            insert_experiment(conn, exp)
            counts["experiments"] += 1

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
                new_id = insert_substance(conn, sub)
                substance_id_map[name] = new_id
                # Add the normalized name as an alias
                add_substance_alias(conn, name, new_id)
                counts["substances"] += 1

        # 4. Insert stimuli (resolve substance_id references)
        for stim in structured.get("stimuli", []):
            stim["paper_id"] = paper_id
            # Resolve substance_id if it was a name reference
            sub_ref = stim.get("substance_name", stim.get("normalized_name", ""))
            if sub_ref and not stim.get("substance_id"):
                resolved = (
                    substance_id_map.get(sub_ref.lower().strip()) or
                    resolve_substance_by_alias(conn, sub_ref) or
                    resolve_substance_by_name(conn, sub_ref)
                )
                if resolved:
                    stim["substance_id"] = resolved
            insert_stimulus(conn, stim)
            counts["stimuli"] += 1

        # 5. Insert samples
        for sample in structured.get("samples", []):
            sample["paper_id"] = paper_id
            insert_sample(conn, sample)
            counts["samples"] += 1

        # 6. Insert sample_components
        for comp in structured.get("sample_components", []):
            insert_sample_component(conn, comp)
            counts["sample_components"] += 1

        # 7. Insert results (with run_id)
        results = structured.get("results", [])
        for r in results:
            r["paper_id"] = paper_id
            r["run_id"] = run_id
        counts["results"] = insert_results_batch(conn, results)

    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()

    return counts


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
