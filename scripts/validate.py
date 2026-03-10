#!/usr/bin/env python3
"""Validate LLM extraction output against gold-standard JSONs.

Supports two modes:
  python scripts/validate.py <extracted.json> <gold_standard.json>   # JSON vs JSON
  python scripts/validate.py --db <paper_id> [--gold <gold.json>]    # SQLite vs gold JSON
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def validate_extraction(extracted: dict, gold_standard: dict) -> dict:
    """Compare an extracted JSON against a gold-standard JSON.

    Returns a detailed accuracy report.
    """
    report = {
        "study_id": extracted.get("study_metadata", {}).get("study_id", "unknown"),
        "overall_accuracy": 0.0,
        "metadata_accuracy": {},
        "experiment_accuracy": [],
        "errors": [],
        "summary": {},
    }

    # 1. Metadata accuracy
    meta_report = _compare_metadata(
        extracted.get("study_metadata", {}),
        gold_standard.get("study_metadata", {}),
    )
    report["metadata_accuracy"] = meta_report

    # 2. Experiment-level accuracy
    ext_experiments = extracted.get("experiments", [])
    gold_experiments = gold_standard.get("experiments", [])

    report["summary"]["num_experiments_extracted"] = len(ext_experiments)
    report["summary"]["num_experiments_gold"] = len(gold_experiments)

    for i, (ext_exp, gold_exp) in enumerate(zip(ext_experiments, gold_experiments)):
        exp_report = _compare_experiment(ext_exp, gold_exp)
        report["experiment_accuracy"].append(exp_report)

    # Handle missing/extra experiments
    if len(ext_experiments) > len(gold_experiments):
        for j in range(len(gold_experiments), len(ext_experiments)):
            report["errors"].append({
                "type": "extra_experiment",
                "experiment_index": j,
                "message": f"Extracted experiment {j} not in gold standard",
            })
    elif len(ext_experiments) < len(gold_experiments):
        for j in range(len(ext_experiments), len(gold_experiments)):
            report["errors"].append({
                "type": "missing_experiment",
                "experiment_index": j,
                "message": f"Gold standard experiment {j} not extracted",
            })

    # Calculate overall accuracy
    total_checks = meta_report.get("total_fields", 0)
    correct_checks = meta_report.get("correct_fields", 0)
    for exp_rep in report["experiment_accuracy"]:
        total_checks += exp_rep.get("total_fields", 0)
        correct_checks += exp_rep.get("correct_fields", 0)

    report["overall_accuracy"] = correct_checks / total_checks if total_checks > 0 else 0.0
    report["summary"]["total_fields_checked"] = total_checks
    report["summary"]["correct_fields"] = correct_checks

    return report


def _compare_metadata(extracted: dict, gold: dict) -> dict:
    """Compare study_metadata fields."""
    fields_to_check = [
        "study_id", "doi", "title", "year", "journal", "country",
        "food_category",
    ]

    total = 0
    correct = 0
    errors = []

    for field in fields_to_check:
        ext_val = extracted.get(field)
        gold_val = gold.get(field)

        if gold_val is None:
            continue  # Skip fields not in gold standard

        total += 1
        if _values_match(ext_val, gold_val):
            correct += 1
        else:
            errors.append({
                "field": field,
                "extracted": ext_val,
                "gold": gold_val,
                "type": "wrong_value" if ext_val is not None else "missing_value",
            })

    # Check authors
    ext_authors = set(a.lower() for a in extracted.get("authors", []))
    gold_authors = set(a.lower() for a in gold.get("authors", []))
    total += 1
    if ext_authors == gold_authors:
        correct += 1
    else:
        errors.append({
            "field": "authors",
            "extracted": list(ext_authors),
            "gold": list(gold_authors),
            "type": "wrong_value",
        })

    return {
        "total_fields": total,
        "correct_fields": correct,
        "accuracy": correct / total if total > 0 else 0.0,
        "errors": errors,
    }


def _compare_experiment(extracted: dict, gold: dict) -> dict:
    """Compare experiment-level data."""
    total = 0
    correct = 0
    errors = []

    # Panel comparison
    ext_panel = extracted.get("panel", {})
    gold_panel = gold.get("panel", {})
    for field in ["panel_type", "panel_size"]:
        ext_val = ext_panel.get(field)
        gold_val = gold_panel.get(field)
        if gold_val is None:
            continue
        total += 1
        if _values_match(ext_val, gold_val):
            correct += 1
        else:
            errors.append({
                "section": "panel",
                "field": field,
                "extracted": ext_val,
                "gold": gold_val,
            })

    # Scale comparison
    ext_scale = extracted.get("scale", {})
    gold_scale = gold.get("scale", {})
    for field in ["type", "range"]:
        ext_val = ext_scale.get(field)
        gold_val = gold_scale.get(field)
        if gold_val is None:
            continue
        total += 1
        if _values_match(ext_val, gold_val):
            correct += 1
        else:
            errors.append({
                "section": "scale",
                "field": field,
                "extracted": ext_val,
                "gold": gold_val,
            })

    # Stimuli count
    ext_stimuli = extracted.get("stimuli", [])
    gold_stimuli = gold.get("stimuli", [])
    total += 1
    if len(ext_stimuli) == len(gold_stimuli):
        correct += 1
    else:
        errors.append({
            "section": "stimuli",
            "field": "count",
            "extracted": len(ext_stimuli),
            "gold": len(gold_stimuli),
        })

    # Numerical data comparison (sensory_data)
    num_report = _compare_numerical_data(
        extracted.get("sensory_data", {}),
        gold.get("sensory_data", {}),
    )
    total += num_report["total"]
    correct += num_report["correct"]
    errors.extend(num_report["errors"])

    return {
        "experiment_id": extracted.get("experiment_id", gold.get("experiment_id", "unknown")),
        "total_fields": total,
        "correct_fields": correct,
        "accuracy": correct / total if total > 0 else 0.0,
        "errors": errors,
    }


def _compare_numerical_data(extracted: dict, gold: dict, path: str = "") -> dict:
    """Recursively compare numerical values in sensory data."""
    total = 0
    correct = 0
    errors = []

    if isinstance(gold, (int, float)) and isinstance(extracted, (int, float)):
        total += 1
        if _numbers_match(extracted, gold):
            correct += 1
        else:
            errors.append({
                "path": path,
                "extracted": extracted,
                "gold": gold,
                "type": "wrong_numerical_value",
            })
    elif isinstance(gold, dict) and isinstance(extracted, dict):
        for key in gold:
            if key in extracted:
                sub = _compare_numerical_data(
                    extracted[key], gold[key], f"{path}.{key}"
                )
                total += sub["total"]
                correct += sub["correct"]
                errors.extend(sub["errors"])
            else:
                # Count missing numerical values
                num_count = _count_numbers(gold[key])
                if num_count > 0:
                    total += num_count
                    errors.append({
                        "path": f"{path}.{key}",
                        "type": "missing_key",
                        "gold_numbers_lost": num_count,
                    })
    elif isinstance(gold, list) and isinstance(extracted, list):
        for i, (ev, gv) in enumerate(zip(extracted, gold)):
            sub = _compare_numerical_data(ev, gv, f"{path}[{i}]")
            total += sub["total"]
            correct += sub["correct"]
            errors.extend(sub["errors"])

    return {"total": total, "correct": correct, "errors": errors}


def _values_match(extracted, gold) -> bool:
    """Check if two values match (case-insensitive for strings)."""
    if isinstance(extracted, str) and isinstance(gold, str):
        return extracted.strip().lower() == gold.strip().lower()
    return extracted == gold


def _numbers_match(extracted: float, gold: float, tolerance: float = 0.05) -> bool:
    """Check if two numbers match within a relative tolerance (5%)."""
    if gold == 0:
        return abs(extracted) < 0.01
    return abs(extracted - gold) / abs(gold) <= tolerance


def _count_numbers(data) -> int:
    """Count numerical values in a nested structure."""
    if isinstance(data, (int, float)):
        return 1
    if isinstance(data, dict):
        return sum(_count_numbers(v) for v in data.values())
    if isinstance(data, list):
        return sum(_count_numbers(v) for v in data)
    return 0


def print_report(report: dict):
    """Print a human-readable validation report.

    Handles both the legacy JSON-vs-JSON report shape and the v4
    DB-vs-gold report that includes per-table and per-source stats.
    """
    study = report.get("study_id", report.get("paper_id", "unknown"))
    print(f"\n📊 Validation Report: {study}")
    print(f"{'=' * 60}")

    print(f"\n  Overall accuracy: {report['overall_accuracy']:.1%}")
    summary = report.get("summary", {})
    print(f"  Fields checked: {summary.get('total_fields_checked', 'N/A')}")
    print(f"  Correct: {summary.get('correct_fields', 'N/A')}")

    if "num_experiments_extracted" in summary:
        print(f"  Experiments: {summary['num_experiments_extracted']}/{summary['num_experiments_gold']}")

    # ── Per-table precision/recall (v4 DB mode) ─────────────
    table_stats = report.get("table_stats")
    if table_stats:
        print(f"\n  {'Table':<18} {'Prec':>6} {'Rec':>6} {'F1':>6}  {'TP':>4} {'FP':>4} {'FN':>4}")
        print(f"  {'-' * 54}")
        for tbl, s in table_stats.items():
            prec = s["precision"]
            rec = s["recall"]
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
            print(f"  {tbl:<18} {prec:>5.1%} {rec:>5.1%} {f1:>5.1%}  {s['tp']:>4} {s['fp']:>4} {s['fn']:>4}")

    # ── Per-source_type stats (v4 DB mode) ──────────────────
    source_stats = report.get("source_stats")
    if source_stats:
        print(f"\n  Results by source_type:")
        for src, s in source_stats.items():
            prec = s["precision"]
            rec = s["recall"]
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
            print(f"    {src:<12} P={prec:.1%}  R={rec:.1%}  F1={f1:.1%}  (TP={s['tp']} FP={s['fp']} FN={s['fn']})")

    # ── Legacy metadata accuracy ────────────────────────────
    meta = report.get("metadata_accuracy")
    if meta and isinstance(meta, dict) and "accuracy" in meta:
        print(f"\n  Metadata: {meta['accuracy']:.1%} ({meta['correct_fields']}/{meta['total_fields']})")
        for err in meta.get("errors", []):
            print(f"    ✗ {err['field']}: got '{err['extracted']}', expected '{err['gold']}'")

    # ── Legacy experiment accuracy ──────────────────────────
    for exp_rep in report.get("experiment_accuracy", []):
        print(f"\n  {exp_rep['experiment_id']}: {exp_rep['accuracy']:.1%} ({exp_rep['correct_fields']}/{exp_rep['total_fields']})")
        for err in exp_rep.get("errors", [])[:10]:
            print(f"    ✗ {err.get('section', '')}.{err.get('field', err.get('path', ''))}: "
                  f"got {err.get('extracted', 'N/A')}, expected {err.get('gold', 'N/A')}")

    # ── Structural / detail errors ──────────────────────────
    for err in report.get("errors", []):
        msg = err.get("message", err.get("type", str(err)))
        print(f"    ✗ {msg}")


# ── v4 DB-based validation ───────────────────────────────────


def _resolve_gold_path(paper_id: str, gold_path: str | Path | None = None) -> Path:
    """Resolve the gold-standard JSON path for a paper."""
    if gold_path:
        return Path(gold_path)
    candidate = ROOT_DIR / "data" / "gold_standard" / f"{paper_id}_extraction.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"No gold standard found for '{paper_id}'. "
        f"Expected at {candidate} or pass --gold explicitly."
    )


def validate_against_db(paper_id: str, gold_path: str | Path | None = None,
                        config: dict = None) -> dict:
    """Compare SQLite data for *paper_id* against a gold-standard JSON.

    Returns a report dict with overall accuracy, per-table precision/recall,
    and per-source_type stats.
    """
    from scripts.db import (
        get_db, get_paper, get_paper_experiments,
        get_paper_results, get_paper_stimuli, get_paper_samples,
    )

    gold_path = _resolve_gold_path(paper_id, gold_path)
    with open(gold_path) as f:
        gold = json.load(f)

    if config is None:
        config = load_config()

    conn = get_db(config)
    try:
        paper = get_paper(conn, paper_id)
        if paper is None:
            return {
                "paper_id": paper_id,
                "overall_accuracy": 0.0,
                "errors": [{"type": "missing_paper", "message": f"Paper '{paper_id}' not in database"}],
                "summary": {"total_fields_checked": 0, "correct_fields": 0},
            }

        experiments = get_paper_experiments(conn, paper_id)
        stimuli = get_paper_stimuli(conn, paper_id)
        samples = get_paper_samples(conn, paper_id)
        results = get_paper_results(conn, paper_id)
    finally:
        conn.close()

    report = {
        "paper_id": paper_id,
        "overall_accuracy": 0.0,
        "table_stats": {},
        "source_stats": {},
        "metadata_accuracy": {},
        "errors": [],
        "summary": {},
    }

    # ── Papers table ────────────────────────────────────────
    report["table_stats"]["papers"] = _compare_paper_row(paper, gold.get("study_metadata", {}))

    # ── Metadata detail (reuse legacy helper for field-level errors) ──
    meta_from_db = {
        "study_id": paper.get("paper_id"),
        "doi": paper.get("doi"),
        "title": paper.get("title"),
        "year": paper.get("year"),
        "journal": paper.get("journal"),
        "country": paper.get("country"),
        "food_category": paper.get("food_category"),
    }
    report["metadata_accuracy"] = _compare_metadata(meta_from_db, gold.get("study_metadata", {}))

    # ── Experiments table ───────────────────────────────────
    gold_experiments = gold.get("experiments", [])
    report["table_stats"]["experiments"] = _compare_row_sets(
        extracted=[e.get("experiment_id") for e in experiments],
        gold=[e.get("experiment_id") for e in gold_experiments],
    )
    report["summary"]["num_experiments_extracted"] = len(experiments)
    report["summary"]["num_experiments_gold"] = len(gold_experiments)

    # ── Stimuli table ───────────────────────────────────────
    gold_stimuli_ids = set()
    for gexp in gold_experiments:
        for s in gexp.get("stimuli", []):
            gold_stimuli_ids.add(s.get("stimulus_id", s.get("name", "")))
    ext_stimuli_ids = {s.get("stimulus_id") or s.get("original_name", "") for s in stimuli}
    report["table_stats"]["stimuli"] = _compare_row_sets(
        extracted=list(ext_stimuli_ids),
        gold=list(gold_stimuli_ids),
    )

    # ── Samples table ──────────────────────────────────────
    gold_sample_ids = set()
    for gexp in gold_experiments:
        for s in gexp.get("stimuli", []):
            sid = s.get("stimulus_id", s.get("name", ""))
            for conc in s.get("concentrations_pct_wv", s.get("concentrations", [])):
                gold_sample_ids.add(f"{sid}@{conc}")
    ext_sample_ids = {s.get("sample_id", s.get("sample_label", "")) for s in samples}
    report["table_stats"]["samples"] = _compare_row_sets(
        extracted=list(ext_sample_ids),
        gold=list(gold_sample_ids),
    )

    # ── Results table — the core comparison ─────────────────
    results_report = compare_results(results, gold)
    report["table_stats"]["results"] = results_report["overall"]
    report["source_stats"] = results_report["by_source"]

    # ── Roll up overall accuracy ────────────────────────────
    total_tp = total_fp = total_fn = 0
    for tbl_s in report["table_stats"].values():
        total_tp += tbl_s["tp"]
        total_fp += tbl_s["fp"]
        total_fn += tbl_s["fn"]

    total = total_tp + total_fp + total_fn
    report["overall_accuracy"] = total_tp / total if total > 0 else 0.0
    report["summary"]["total_fields_checked"] = total_tp + total_fn  # gold count
    report["summary"]["correct_fields"] = total_tp

    return report


def _compare_paper_row(paper: dict, gold_meta: dict) -> dict:
    """Compare the papers table row against gold study_metadata. Returns TP/FP/FN."""
    field_map = {
        "doi": "doi", "title": "title", "year": "year",
        "journal": "journal", "country": "country",
        "food_category": "food_category",
    }
    tp = fp = fn = 0
    for db_col, gold_key in field_map.items():
        gold_val = gold_meta.get(gold_key)
        ext_val = paper.get(db_col)
        if gold_val is None:
            continue
        if ext_val is not None and _values_match(ext_val, gold_val):
            tp += 1
        elif ext_val is not None:
            fp += 1
            fn += 1  # wrong value: counts as both a spurious and a miss
        else:
            fn += 1
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec}


def _compare_row_sets(extracted: list, gold: list) -> dict:
    """Compare two lists of identifiers (e.g., experiment_ids). Returns TP/FP/FN."""
    ext_set = set(str(x) for x in extracted if x is not None)
    gold_set = set(str(x) for x in gold if x is not None)
    tp = len(ext_set & gold_set)
    fp = len(ext_set - gold_set)
    fn = len(gold_set - ext_set)
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec}


def compare_results(db_results: list[dict], gold: dict,
                    tolerance: float = 0.05) -> dict:
    """Compare DB result rows against gold-standard sensory data.

    Builds a set of (experiment, stimulus, attribute, concentration, value) tuples
    from both sources and computes precision/recall overall and per source_type.

    Returns::

        {
            "overall": {"tp": …, "fp": …, "fn": …, "precision": …, "recall": …},
            "by_source": {
                "table": {"tp": …, …},
                "figure": {…},
                "text": {…},
            },
        }
    """
    # ── Build gold tuples: (experiment_id, stimulus, attribute, concentration, value) ──
    gold_tuples = []
    for gexp in gold.get("experiments", []):
        exp_id = gexp.get("experiment_id", "")
        sd = gexp.get("sensory_data", {})
        gold_tuples.extend(_flatten_gold_sensory_data(exp_id, sd))

    # ── Build extracted tuples from DB rows ─────────────────
    ext_tuples = []
    for r in db_results:
        key = _result_key(r)
        source = (r.get("source_type") or "unknown").lower()
        ext_tuples.append((key, source))

    # ── Match: gold tuple matched if any extracted tuple is close ──
    gold_matched = [False] * len(gold_tuples)
    ext_matched = [False] * len(ext_tuples)

    for gi, (gkey, gsource) in enumerate(gold_tuples):
        for ei, (ekey, esource) in enumerate(ext_tuples):
            if ext_matched[ei]:
                continue
            if _keys_match(ekey, gkey, tolerance):
                gold_matched[gi] = True
                ext_matched[ei] = True
                break

    tp = sum(gold_matched)
    fn = len(gold_tuples) - tp
    fp = len(ext_tuples) - sum(ext_matched)

    overall = _prec_rec(tp, fp, fn)

    # ── Per source_type ─────────────────────────────────────
    source_buckets = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})

    for gi, (gkey, gsource) in enumerate(gold_tuples):
        bucket = gsource if gsource in ("table", "figure", "text") else "other"
        if gold_matched[gi]:
            source_buckets[bucket]["tp"] += 1
        else:
            source_buckets[bucket]["fn"] += 1

    for ei, (ekey, esource) in enumerate(ext_tuples):
        bucket = esource if esource in ("table", "figure", "text") else "other"
        if not ext_matched[ei]:
            source_buckets[bucket]["fp"] += 1

    by_source = {}
    for src, counts in source_buckets.items():
        by_source[src] = _prec_rec(counts["tp"], counts["fp"], counts["fn"])

    return {"overall": overall, "by_source": by_source}


def _prec_rec(tp: int, fp: int, fn: int) -> dict:
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec}


def _result_key(row: dict) -> tuple:
    """Build a comparison key from a DB result row."""
    return (
        str(row.get("experiment_id", "")),
        str(row.get("sample_id", "")),
        str(row.get("attribute_raw") or row.get("attribute_normalized") or ""),
        row.get("value"),
    )


def _flatten_gold_sensory_data(experiment_id: str, sd: dict) -> list[tuple]:
    """Walk gold-standard sensory_data and yield (key_tuple, source_type) pairs.

    Handles the two known gold-standard shapes:
      - wee2018 style: sd["dose_response_data"] → list of per-stimulus dicts
      - benabu2018 style: sd["raw_scores"]["data"] → list of per-stimulus dicts
    """
    tuples = []

    # ── wee2018 shape: dose_response_data ───────────────────
    for drd in sd.get("dose_response_data", []):
        stim = drd.get("stimulus_id", "")
        source = _classify_source(drd.get("source", ""))
        for pt in drd.get("data_points", []):
            for k, v in pt.items():
                if isinstance(v, (int, float)) and k not in ("concentration_pct_wv",):
                    key = (experiment_id, stim, k, v)
                    tuples.append((key, source))

    # ── benabu2018 shape: raw_scores.data, relative_perceptions.data ──
    for section_key in ("raw_scores", "relative_perceptions", "sweetness_of_pure_sugars"):
        section = sd.get(section_key)
        if not isinstance(section, dict):
            continue
        source = _classify_source(section.get("source", ""))
        for item in section.get("data", []):
            if not isinstance(item, dict):
                continue
            stim = item.get("stimulus_id", item.get("stimulus", ""))
            for attr, val in item.items():
                if attr in ("stimulus_id", "stimulus"):
                    continue
                if isinstance(val, (int, float)):
                    key = (experiment_id, stim, attr, val)
                    tuples.append((key, source))
                elif isinstance(val, dict):
                    for stat_key, stat_val in val.items():
                        if isinstance(stat_val, (int, float)):
                            attr_full = f"{attr}.{stat_key}"
                            key = (experiment_id, stim, attr_full, stat_val)
                            tuples.append((key, source))

    return tuples


def _classify_source(source_str: str) -> str:
    """Map a gold-standard 'source' string to a source_type category."""
    if not source_str:
        return "unknown"
    s = source_str.lower()
    if "figure" in s or "fig" in s:
        return "figure"
    if "table" in s:
        return "table"
    if "text" in s or "section" in s or "inline" in s:
        return "text"
    return "other"


def _keys_match(ext_key: tuple, gold_key: tuple, tolerance: float = 0.05) -> bool:
    """Check if an extracted result key matches a gold key.

    Keys are (experiment_id, stimulus/sample, attribute, value).
    experiment_id and attribute are compared case-insensitively;
    value is compared with numerical tolerance.
    """
    e_exp, e_stim, e_attr, e_val = ext_key
    g_exp, g_stim, g_attr, g_val = gold_key

    # experiment must match
    if e_exp.lower() != g_exp.lower():
        return False

    # attribute must match (strip, lowercase)
    if not _values_match(e_attr, g_attr):
        return False

    # value must be numerically close
    if isinstance(e_val, (int, float)) and isinstance(g_val, (int, float)):
        if not _numbers_match(e_val, g_val, tolerance):
            return False
    elif e_val != g_val:
        return False

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Validate extraction output against gold-standard JSONs.",
    )
    parser.add_argument("files", nargs="*",
                        help="<extracted.json> <gold_standard.json> for JSON mode")
    parser.add_argument("--db", metavar="PAPER_ID",
                        help="Validate SQLite data for PAPER_ID against gold standard")
    parser.add_argument("--gold", metavar="GOLD_JSON",
                        help="Path to gold-standard JSON (auto-resolved if omitted)")
    args = parser.parse_args()

    if args.db:
        # ── v4 DB mode ──────────────────────────────────────
        config = load_config()
        report = validate_against_db(args.db, gold_path=args.gold, config=config)
        print_report(report)
        report_path = ROOT_DIR / "data" / "extractions" / f"{args.db}.validation.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Report saved: {report_path}")

    elif len(args.files) >= 2:
        # ── Legacy JSON-vs-JSON mode ────────────────────────
        ext_path = Path(args.files[0])
        gold_path = Path(args.files[1])

        with open(ext_path) as f:
            extracted = json.load(f)
        with open(gold_path) as f:
            gold = json.load(f)

        report = validate_extraction(extracted, gold)
        print_report(report)

        report_path = ext_path.with_suffix(".validation.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Report saved: {report_path}")

    elif len(args.files) == 1:
        # ── Single file: auto-detect gold standard by study_id ──
        ext_path = Path(args.files[0])
        with open(ext_path) as f:
            extracted = json.load(f)
        study_id = extracted.get("study_metadata", {}).get("study_id", "")
        gold_path = _resolve_gold_path(study_id, args.gold)
        with open(gold_path) as f:
            gold = json.load(f)

        report = validate_extraction(extracted, gold)
        print_report(report)

        report_path = ext_path.with_suffix(".validation.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Report saved: {report_path}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
