#!/usr/bin/env python3
"""
Run Agent 1 (free extraction) only on a single paper.

Usage:
    python scripts/run_agent1.py data/html/paper.html
    python scripts/run_agent1.py data/html/paper.html --doi "10.1093/chemse/..."
    python scripts/run_agent1.py data/html/paper.html --output custom_output.json
"""

import argparse
import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.parse_article import parse_article
from scripts.agent1_extract import run_agent1, save_agent1_output
from scripts.paper_id import paper_id_from_filename, doi_to_paper_id
from scripts.llm_extract import LLMClient

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="Run Agent 1 (free extraction) only on a single paper."
    )
    parser.add_argument(
        "paper_path",
        type=str,
        help="Path to the HTML/PDF paper file"
    )
    parser.add_argument(
        "--doi",
        type=str,
        default=None,
        help="DOI of the paper (used to generate paper_id)"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Custom output path for JSON (default: data/extractions/parts/<paper_id>/agent1_extraction.json)"
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the JSON output to stdout"
    )
    args = parser.parse_args()

    # Validate paper path
    paper_path = Path(args.paper_path)
    if not paper_path.exists():
        console.print(f"[red]✗ File not found:[/red] {paper_path}")
        sys.exit(1)

    # Load config
    config_path = ROOT_DIR / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Create LLM client
    llm = LLMClient(config)

    # Generate paper_id
    if args.doi:
        paper_id = doi_to_paper_id(args.doi)
        console.print(f"[dim]Paper ID (from DOI):[/dim] {paper_id}")
    else:
        paper_id = paper_id_from_filename(str(paper_path))
        console.print(f"[dim]Paper ID (from filename):[/dim] {paper_id}")

    # Parse article
    console.print(f"\n[bold]Parsing article...[/bold]")
    article = parse_article(paper_path)
    console.print(f"  Title: {article.title[:80]}..." if article.title and len(article.title) > 80 else f"  Title: {article.title}")
    console.print(f"  Tables: {len(article.tables)}")
    console.print(f"  Figures: {len(article.figures)}")
    console.print(f"  Sections: {list(article.sections.keys())}")

    # Run Agent 1
    console.print(f"\n[bold]Running Agent 1 (free extraction)...[/bold]")
    result = run_agent1(article, paper_id, config, llm)

    # Save output
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        console.print(f"\n[green]✓ Saved to:[/green] {output_path}")
    else:
        output_path = save_agent1_output(result, paper_id, config)
        console.print(f"\n[green]✓ Saved to:[/green] {output_path}")

    # Print summary
    console.print(f"\n[bold]Summary:[/bold]")
    exps = result.get("experiments", [])
    console.print(f"  Experiments: {len(exps)}")
    for i, exp in enumerate(exps):
        exp_id = exp.get("experiment_id", f"exp{i+1}")
        stimuli = len(exp.get("stimuli", []))
        samples = len(exp.get("samples", []))
        sensory_blocks = len(exp.get("sensory_data", []))
        derived = len(exp.get("derived_metrics", []))
        console.print(f"    {exp_id}: {stimuli} stimuli, {samples} samples, {sensory_blocks} sensory_data blocks, {derived} derived_metrics")

    # Print cost
    console.print()
    llm.print_cost_summary()

    # Optionally print JSON
    if args.print_json:
        console.print(f"\n[bold]JSON Output:[/bold]")
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
