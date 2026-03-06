#!/usr/bin/env python3
"""Validate LLM extraction output against gold-standard JSONs."""

import json
import sys
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


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
    """Print a human-readable validation report."""
    print(f"\n📊 Validation Report: {report['study_id']}")
    print(f"{'=' * 50}")

    print(f"\n  Overall accuracy: {report['overall_accuracy']:.1%}")
    print(f"  Fields checked: {report['summary']['total_fields_checked']}")
    print(f"  Correct: {report['summary']['correct_fields']}")
    print(f"  Experiments: {report['summary']['num_experiments_extracted']}/{report['summary']['num_experiments_gold']}")

    meta = report["metadata_accuracy"]
    print(f"\n  Metadata: {meta['accuracy']:.1%} ({meta['correct_fields']}/{meta['total_fields']})")
    for err in meta.get("errors", []):
        print(f"    ✗ {err['field']}: got '{err['extracted']}', expected '{err['gold']}'")

    for exp_rep in report["experiment_accuracy"]:
        print(f"\n  {exp_rep['experiment_id']}: {exp_rep['accuracy']:.1%} ({exp_rep['correct_fields']}/{exp_rep['total_fields']})")
        for err in exp_rep.get("errors", [])[:10]:  # Show first 10 errors
            print(f"    ✗ {err.get('section', '')}.{err.get('field', err.get('path', ''))}: "
                  f"got {err.get('extracted', 'N/A')}, expected {err.get('gold', 'N/A')}")

    if report["errors"]:
        print(f"\n  Structural errors:")
        for err in report["errors"]:
            print(f"    ✗ {err['message']}")


def main():
    if len(sys.argv) < 3:
        print("Usage: python validate.py <extracted.json> <gold_standard.json>")
        sys.exit(1)

    ext_path = Path(sys.argv[1])
    gold_path = Path(sys.argv[2])

    with open(ext_path) as f:
        extracted = json.load(f)
    with open(gold_path) as f:
        gold = json.load(f)

    report = validate_extraction(extracted, gold)
    print_report(report)

    # Save report
    report_path = ext_path.with_suffix(".validation.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    main()
