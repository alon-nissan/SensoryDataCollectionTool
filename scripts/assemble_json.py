#!/usr/bin/env python3
"""Assemble complete paper JSON from individual LLM extraction outputs."""

import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def assemble_json(
    study_id: str,
    metadata: dict,
    experiments_design: dict,
    stimuli: dict,
    sensory_data: dict,
    figure_data: list[dict] = None,
    figure_inventory: list[dict] = None,
) -> dict:
    """Assemble a complete paper JSON from individual prompt outputs.

    Args:
        study_id: Paper identifier
        metadata: Output from Prompt A (study_metadata)
        experiments_design: Output from Prompt B (experiments with panel/session/scale)
        stimuli: Output from Prompt C (experiments with stimuli arrays)
        sensory_data: Output from Prompt D (experiments with sensory_data/derived_metrics)
        figure_data: Output from Prompt E (list of figure extraction results)
        figure_inventory: List of figure metadata dicts

    Returns:
        Complete paper JSON dict
    """
    # Start with metadata
    paper = {
        "study_metadata": metadata.get("study_metadata", metadata),
    }

    # Merge experiment sections: design + stimuli + sensory_data
    experiments = _merge_experiments(experiments_design, stimuli, sensory_data)
    paper["experiments"] = experiments

    # Add figure data to relevant experiments if available
    if figure_data:
        _integrate_figure_data(paper, figure_data)

    # Cross-experiment data (if any experiments share data)
    paper["cross_experiment_data"] = _extract_cross_experiment(sensory_data)

    # Figure inventory
    paper["figure_inventory"] = figure_inventory or []

    # Extraction metadata
    paper["extraction_metadata"] = _build_extraction_metadata(paper)

    return paper


def _merge_experiments(design: dict, stimuli: dict, sensory: dict) -> list:
    """Merge experiment data from three prompts by experiment_id."""
    experiments = {}

    # Start with design (Prompt B)
    for exp in design.get("experiments", []):
        exp_id = exp.get("experiment_id", "unknown")
        experiments[exp_id] = exp

    # Merge stimuli (Prompt C)
    for exp in stimuli.get("experiments", []):
        exp_id = exp.get("experiment_id", "unknown")
        if exp_id in experiments:
            experiments[exp_id]["stimuli"] = exp.get("stimuli", [])
        else:
            experiments[exp_id] = exp

    # Merge sensory data (Prompt D)
    for exp in sensory.get("experiments", []):
        exp_id = exp.get("experiment_id", "unknown")
        if exp_id in experiments:
            experiments[exp_id]["sensory_data"] = exp.get("sensory_data", {})
            experiments[exp_id]["derived_metrics"] = exp.get("derived_metrics", {})
            experiments[exp_id]["statistical_outputs"] = exp.get("statistical_outputs", {})
        else:
            experiments[exp_id] = exp

    return list(experiments.values())


def _integrate_figure_data(paper: dict, figure_data: list[dict]):
    """Integrate figure extraction results into the paper JSON."""
    if not figure_data:
        return

    # Add figure data to extraction metadata
    paper.setdefault("figure_extractions", [])
    for fig in figure_data:
        paper["figure_extractions"].append(fig)


def _extract_cross_experiment(sensory_data: dict) -> dict:
    """Extract any data that spans multiple experiments."""
    return sensory_data.get("cross_experiment_data", {})


def _build_extraction_metadata(paper: dict) -> dict:
    """Build the extraction_metadata section with provenance and data gap info."""
    data_gaps = []
    total_values = 0
    null_values = 0

    # Count data completeness
    for exp in paper.get("experiments", []):
        # Check panel completeness
        panel = exp.get("panel", {})
        for field in ["panel_type", "panel_size"]:
            total_values += 1
            if panel.get(field) is None:
                null_values += 1
                data_gaps.append({
                    "experiment": exp.get("experiment_id"),
                    "field": f"panel.{field}",
                    "reason": "not reported in paper",
                })

        # Check if sensory_data exists
        if not exp.get("sensory_data"):
            data_gaps.append({
                "experiment": exp.get("experiment_id"),
                "field": "sensory_data",
                "reason": "no sensory data extracted — may be in figures only",
            })

        # Check stimuli
        if not exp.get("stimuli"):
            data_gaps.append({
                "experiment": exp.get("experiment_id"),
                "field": "stimuli",
                "reason": "no stimuli extracted",
            })

    return {
        "extraction_date": datetime.now().isoformat(),
        "extraction_method": "llm_pipeline_v1",
        "models_used": {
            "text": "claude-sonnet-4-20250514",
            "vision": "claude-opus-4-20250514",
        },
        "data_gaps": data_gaps,
        "num_data_gaps": len(data_gaps),
        "validation_status": "pending",
    }


def save_json(paper: dict, output_dir: Path = None) -> Path:
    """Save assembled JSON to the extractions directory."""
    config = load_config()
    if output_dir is None:
        output_dir = ROOT_DIR / config["paths"]["extractions_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    study_id = paper.get("study_metadata", {}).get("study_id", "unknown")
    output_path = output_dir / f"{study_id}.json"

    with open(output_path, "w") as f:
        json.dump(paper, f, indent=2, ensure_ascii=False)

    print(f"  💾 Saved: {output_path}")
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python assemble_json.py <study_id>")
        print("Assembles JSON from individual extraction outputs in data/extractions/parts/")
        sys.exit(1)

    study_id = sys.argv[1]
    config = load_config()
    parts_dir = ROOT_DIR / config["paths"]["extractions_dir"] / "parts" / study_id

    if not parts_dir.exists():
        print(f"No extraction parts found at {parts_dir}")
        sys.exit(1)

    # Load individual parts
    def load_part(name):
        path = parts_dir / f"{name}.json"
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return {}

    metadata = load_part("metadata")
    experiments_design = load_part("experiments")
    stimuli = load_part("stimuli")
    sensory_data = load_part("sensory_data")

    # Load figure data if available
    figure_data = []
    figures_dir = parts_dir / "figures"
    if figures_dir.exists():
        for fig_path in sorted(figures_dir.glob("*.json")):
            with open(fig_path) as f:
                figure_data.append(json.load(f))

    paper = assemble_json(
        study_id=study_id,
        metadata=metadata,
        experiments_design=experiments_design,
        stimuli=stimuli,
        sensory_data=sensory_data,
        figure_data=figure_data,
    )

    save_json(paper)
    print(f"\n✅ Assembled JSON for {study_id}")
    print(f"  Experiments: {len(paper['experiments'])}")
    print(f"  Data gaps: {paper['extraction_metadata']['num_data_gaps']}")


if __name__ == "__main__":
    main()
