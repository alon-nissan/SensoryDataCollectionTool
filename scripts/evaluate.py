#!/usr/bin/env python3
"""Evaluate pipeline extraction quality against human-annotated ground truth.

Annotators read papers directly and fill in data/ground_truth/{paper_id}.csv
from scratch. This module compares those independent annotations against the
pipeline's DB output and reports precision, recall, and F1 overall and broken
down by value_type, source type, attribute, and experiment.

Usage:
    # Generate a blank CSV template to start annotating
    python scripts/evaluate.py --blank-template --paper-id smith2023

    # Evaluate one paper (after saving annotations as {paper_id}.csv)
    python scripts/evaluate.py --paper-id smith2023

    # Evaluate all papers that have ground truth
    python scripts/evaluate.py --all

    # Print summary table across all papers
    python scripts/evaluate.py --all --summary
"""

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from rich.console import Console
from rich.table import Table

from schemas.ground_truth import (
    GROUND_TRUTH_DIR,
    GroundTruthRow,
    generate_blank_template,
    load_ground_truth,
    observations_to_ground_truth,
)
from scripts.db.db import get_db, get_paper_observations, get_paper_observations_with_panels

console = Console()

# ---------------------------------------------------------------------------
# Synonym maps
# ---------------------------------------------------------------------------

# Maps lowercase alias → canonical substance name (with underscores).
# Keys may use either spaces or underscores; both are checked at match time.
SUBSTANCE_SYNONYMS: dict[str, str] = {
    "nacl": "sodium_chloride",
    "sodium chloride": "sodium_chloride",
    "salt": "sodium_chloride",
    "msg": "monosodium_glutamate",
    "monosodium glutamate": "monosodium_glutamate",
    "quinine hcl": "quinine_hydrochloride",
    "quinine hydrochloride": "quinine_hydrochloride",
}


def _load_attribute_synonyms() -> dict[str, str]:
    """Load attribute synonym map from vocabulary/attribute_synonyms.json.

    Returns an empty dict if the file doesn't exist. Populate the file to
    extend the synonym map without touching this code.
    """
    path = ROOT_DIR / "vocabulary" / "attribute_synonyms.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def _norm_substance(name: str, synonyms: dict[str, str]) -> str:
    """Normalise a substance name via the synonym map."""
    key = (name or "").lower().strip()
    key_us = key.replace(" ", "_")
    return synonyms.get(key_us, synonyms.get(key, key_us))


def _norm_attribute(attr: str, synonyms: dict[str, str]) -> str:
    """Normalise an attribute name via the synonym map."""
    key = (attr or "").lower().strip()
    return synonyms.get(key, key)


def _source_category(source_location: str) -> str:
    """Classify source_location as 'table', 'figure', 'text', or 'unknown'."""
    sl = (source_location or "").lower().strip()
    if sl.startswith("figure") or sl.startswith("fig"):
        return "figure"
    if sl.startswith("table"):
        return "table"
    if sl:
        return "text"
    return "unknown"


# ---------------------------------------------------------------------------
# Row-level matching
# ---------------------------------------------------------------------------

def _mixture_key(row: GroundTruthRow, substance_synonyms: dict) -> frozenset:
    """Build a frozenset of (canonical_substance, concentration_rounded) pairs.

    Covers both component slots. Used as the mixture identity key for matching.
    """
    pairs = []
    if row.substance_1:
        sub = _norm_substance(row.substance_1, substance_synonyms)
        pairs.append((sub, round(row.concentration_1, 6) if row.concentration_1 is not None else None))
    if row.substance_2:
        sub = _norm_substance(row.substance_2, substance_synonyms)
        pairs.append((sub, round(row.concentration_2, 6) if row.concentration_2 is not None else None))
    return frozenset(pairs)


def _structurally_match(
    gt: GroundTruthRow,
    pipe: GroundTruthRow,
    substance_syn: dict,
    attribute_syn: dict,
) -> bool:
    """Return True if gt and pipe agree on all structural matching fields.

    Fields checked: mixture set (substance+concentration for each component slot),
    experiment, panel_label, measurement_domain, base_matrix, is_control,
    attribute, value_type.
    """
    if _mixture_key(gt, substance_syn) != _mixture_key(pipe, substance_syn):
        return False
    if (gt.experiment or "").lower() != (pipe.experiment or "").lower():
        return False
    if (gt.panel_label or "").lower() != (pipe.panel_label or "").lower():
        return False
    gt_domain = (gt.measurement_domain or "sensory").lower()
    pipe_domain = (pipe.measurement_domain or "sensory").lower()
    if gt_domain != pipe_domain:
        return False
    gt_matrix = (gt.base_matrix or "").lower() or None
    pipe_matrix = (pipe.base_matrix or "").lower() or None
    if gt_matrix != pipe_matrix:
        return False
    if gt.is_control != pipe.is_control:
        return False
    if _norm_attribute(gt.attribute, attribute_syn) != _norm_attribute(pipe.attribute, attribute_syn):
        return False
    if (gt.value_type or "").lower() != (pipe.value_type or "").lower():
        return False
    return True


def _value_correct(gt: GroundTruthRow, pipe: GroundTruthRow) -> tuple[bool, float]:
    """Check whether the extracted value is within tolerance.

    Tolerance is 10% for figure-sourced data, 1% for everything else.
    Returns (is_correct, abs_pct_diff).
    """
    if gt.value is None or pipe.value is None:
        return (gt.value == pipe.value), 0.0
    tol = 0.10 if _source_category(gt.source_location) == "figure" else 0.01
    denom = max(abs(gt.value), 1e-12)
    pct_diff = abs(gt.value - pipe.value) / denom
    return pct_diff <= tol, round(pct_diff, 6)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_rows(
    gt_rows: list[GroundTruthRow],
    pipe_rows: list[GroundTruthRow],
    substance_synonyms: dict = None,
    attribute_synonyms: dict = None,
) -> dict:
    """Pair ground truth rows with pipeline rows using greedy first-match.

    Returns a dict with:
      matched:               list of (gt_idx, pipe_idx, is_correct, pct_diff)
      unmatched_gt_indices:  gt row indices with no matching pipeline row
      unmatched_pipe_indices: pipeline row indices with no matching gt row
    """
    substance_syn = substance_synonyms if substance_synonyms is not None else SUBSTANCE_SYNONYMS
    attribute_syn = attribute_synonyms if attribute_synonyms is not None else {}

    used_pipe: set[int] = set()
    matched: list[tuple[int, int, bool, float]] = []

    for gt_idx, gt in enumerate(gt_rows):
        for pipe_idx, pipe in enumerate(pipe_rows):
            if pipe_idx in used_pipe:
                continue
            if _structurally_match(gt, pipe, substance_syn, attribute_syn):
                used_pipe.add(pipe_idx)
                correct, pct_diff = _value_correct(gt, pipe)
                matched.append((gt_idx, pipe_idx, correct, pct_diff))
                break

    matched_gt: set[int] = {g for g, _, _, _ in matched}
    unmatched_gt_indices = [i for i in range(len(gt_rows)) if i not in matched_gt]
    unmatched_pipe_indices = [i for i in range(len(pipe_rows)) if i not in used_pipe]

    return {
        "matched": matched,
        "unmatched_gt_indices": unmatched_gt_indices,
        "unmatched_pipe_indices": unmatched_pipe_indices,
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _prf(n_correct_gt: int, n_correct_pipe: int, n_gt: int, n_pipe: int) -> dict:
    """Compute precision, recall, and F1 from counts."""
    precision = n_correct_pipe / n_pipe if n_pipe > 0 else 0.0
    recall = n_correct_gt / n_gt if n_gt > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "n_pipeline": n_pipe,
        "n_ground_truth": n_gt,
        "n_correct": max(n_correct_gt, n_correct_pipe),
    }


def compute_metrics(
    match_result: dict,
    gt_rows: list[GroundTruthRow],
    pipe_rows: list[GroundTruthRow],
) -> dict:
    """Compute precision/recall/F1 overall and by breakdown dimensions.

    For each breakdown group:
      - precision = (correct pipeline rows in group) / (total pipeline rows in group)
      - recall    = (correct gt rows in group)       / (total gt rows in group)

    Returns a dict with overall, by_value_type, by_source, by_attribute, by_experiment.
    """
    matched = match_result["matched"]  # (gt_idx, pipe_idx, correct, pct_diff)

    correct_gt_set: set[int] = {g for g, _, correct, _ in matched if correct}
    correct_pipe_set: set[int] = {p for _, p, correct, _ in matched if correct}

    n_correct_overall = len(correct_gt_set)
    overall = _prf(n_correct_overall, len(correct_pipe_set), len(gt_rows), len(pipe_rows))

    def _breakdown(gt_key, pipe_key) -> dict:
        """PRF for each unique key value, using separate gt/pipe correct counts."""
        gt_keys = {gt_key(r) for r in gt_rows if gt_key(r)}
        pipe_keys = {pipe_key(r) for r in pipe_rows if pipe_key(r)}
        result: dict[str, dict] = {}
        for key in sorted(gt_keys | pipe_keys):
            n_gt_k = sum(1 for r in gt_rows if gt_key(r) == key)
            n_pipe_k = sum(1 for r in pipe_rows if pipe_key(r) == key)
            n_corr_gt = sum(1 for i, r in enumerate(gt_rows) if gt_key(r) == key and i in correct_gt_set)
            n_corr_pipe = sum(1 for i, r in enumerate(pipe_rows) if pipe_key(r) == key and i in correct_pipe_set)
            result[key] = _prf(n_corr_gt, n_corr_pipe, n_gt_k, n_pipe_k)
        return result

    by_value_type = _breakdown(
        lambda r: r.value_type or "",
        lambda r: r.value_type or "",
    )
    by_source = _breakdown(
        lambda r: _source_category(r.source_location),
        lambda r: _source_category(r.source_location),
    )
    by_attribute = _breakdown(
        lambda r: (r.attribute or "").lower().strip(),
        lambda r: (r.attribute or "").lower().strip(),
    )
    by_experiment = _breakdown(
        lambda r: r.experiment or "",
        lambda r: r.experiment or "",
    )
    by_panel = _breakdown(
        lambda r: r.panel_label or "unknown",
        lambda r: r.panel_label or "unknown",
    )
    by_domain = _breakdown(
        lambda r: (r.measurement_domain or "sensory").lower(),
        lambda r: (r.measurement_domain or "sensory").lower(),
    )

    return {
        "overall": overall,
        "by_value_type": by_value_type,
        "by_source": by_source,
        "by_attribute": by_attribute,
        "by_experiment": by_experiment,
        "by_panel": by_panel,
        "by_domain": by_domain,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _row_dict(row: GroundTruthRow) -> dict:
    from dataclasses import asdict
    return asdict(row)


def build_report(
    paper_id: str,
    gt_rows: list[GroundTruthRow],
    pipe_rows: list[GroundTruthRow],
    substance_synonyms: dict = None,
    attribute_synonyms: dict = None,
) -> dict:
    """Build a full evaluation report for one paper.

    Returns a JSON-serialisable dict matching the documented report schema.
    """
    attr_syn = attribute_synonyms if attribute_synonyms is not None else _load_attribute_synonyms()

    match_result = match_rows(gt_rows, pipe_rows, substance_synonyms, attr_syn)
    metrics = compute_metrics(match_result, gt_rows, pipe_rows)
    matched = match_result["matched"]

    unmatched_gt = [
        {"row": _row_dict(gt_rows[i]), "reason": "not found in pipeline output"}
        for i in match_result["unmatched_gt_indices"]
    ]
    unmatched_pipe = [
        {"row": _row_dict(pipe_rows[i]), "reason": "no matching ground truth row"}
        for i in match_result["unmatched_pipe_indices"]
    ]
    value_mismatches = [
        {
            "ground_truth": _row_dict(gt_rows[g]),
            "pipeline": _row_dict(pipe_rows[p]),
            "difference_pct": round(diff * 100, 2),
        }
        for g, p, correct, diff in matched
        if not correct
    ]

    return {
        "paper_id": paper_id,
        **metrics,
        "unmatched_pipeline": unmatched_pipe,
        "unmatched_ground_truth": unmatched_gt,
        "value_mismatches": value_mismatches,
    }


# ---------------------------------------------------------------------------
# DB access helpers
# ---------------------------------------------------------------------------

def _load_pipeline_rows(paper_id: str) -> list[GroundTruthRow]:
    """Load pipeline observations (with panel metadata) from the DB and convert to GroundTruthRow format."""
    conn = get_db()
    try:
        obs = get_paper_observations_with_panels(conn, paper_id)
    finally:
        conn.close()
    return observations_to_ground_truth(obs, paper_id)


# ---------------------------------------------------------------------------
# CLI output helpers
# ---------------------------------------------------------------------------

def _print_summary_table(reports: list[dict]) -> None:
    """Print a summary table across all evaluated papers."""
    table = Table(title="Extraction Evaluation Summary", show_lines=False)
    table.add_column("Paper ID", style="cyan", no_wrap=True)
    table.add_column("GT", justify="right")
    table.add_column("Pipeline", justify="right")
    table.add_column("Correct", justify="right")
    table.add_column("Precision", justify="right", style="green")
    table.add_column("Recall", justify="right", style="yellow")
    table.add_column("F1", justify="right", style="bold")

    for report in reports:
        ov = report["overall"]
        table.add_row(
            report["paper_id"],
            str(ov["n_ground_truth"]),
            str(ov["n_pipeline"]),
            str(ov["n_correct"]),
            f"{ov['precision']:.3f}",
            f"{ov['recall']:.3f}",
            f"{ov['f1']:.3f}",
        )

    console.print(table)


def _output_report(report: dict, output_dir: str | None) -> None:
    """Print a JSON report to stdout or save it to a file."""
    json_str = json.dumps(report, indent=2, default=str)
    if output_dir:
        out_path = Path(output_dir) / f"{report['paper_id']}_eval.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json_str)
        console.print(f"[green]✓[/green] Report: {out_path}")
    else:
        console.print(json_str)


def _evaluate_paper(paper_id: str, attribute_synonyms: dict) -> dict | None:
    """Evaluate one paper. Returns a report dict, or None if no ground truth exists."""
    gt_rows = load_ground_truth(paper_id)
    if not gt_rows:
        console.print(f"[yellow]No ground truth for {paper_id}[/yellow]")
        return None
    pipe_rows = _load_pipeline_rows(paper_id)
    return build_report(paper_id, gt_rows, pipe_rows, attribute_synonyms=attribute_synonyms)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate pipeline extraction quality against ground truth."
    )
    parser.add_argument("--paper-id", type=str, help="Paper ID to evaluate or generate template for")
    parser.add_argument("--all", action="store_true", help="Evaluate all papers with ground truth CSVs")
    parser.add_argument("--summary", action="store_true", help="Print a summary table after evaluation")
    parser.add_argument(
        "--blank-template",
        action="store_true",
        help="Generate a blank CSV template (headers only) for human annotation",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory to write JSON reports (default: print to stdout)",
    )
    args = parser.parse_args()

    # --- Generate blank annotation template ---
    if args.blank_template:
        if not args.paper_id:
            console.print("[red]--paper-id is required with --blank-template[/red]")
            sys.exit(1)
        path = generate_blank_template(args.paper_id)
        console.print(f"[green]✓[/green] Blank template: {path}")
        console.print("[dim]Fill in one row per numeric measurement from the paper.[/dim]")
        console.print(f"[dim]Rename to {args.paper_id}.csv when ready for evaluation.[/dim]")
        return

    attribute_synonyms = _load_attribute_synonyms()

    # --- Single paper ---
    if args.paper_id and not args.all:
        report = _evaluate_paper(args.paper_id, attribute_synonyms)
        if report:
            _output_report(report, args.output_dir)
            if args.summary:
                _print_summary_table([report])
        return

    # --- All papers ---
    if args.all:
        GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
        gt_files = sorted(
            f for f in GROUND_TRUTH_DIR.glob("*.csv") if "_BLANK" not in f.name
        )
        if not gt_files:
            console.print("[yellow]No ground truth files found in data/ground_truth/[/yellow]")
            return

        reports = []
        for gt_file in gt_files:
            paper_id = gt_file.stem
            report = _evaluate_paper(paper_id, attribute_synonyms)
            if report:
                reports.append(report)
                _output_report(report, args.output_dir)

        if args.summary and reports:
            _print_summary_table(reports)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
