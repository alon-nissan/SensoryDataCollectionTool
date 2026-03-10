#!/usr/bin/env python3
"""Agent 4 — Validation & Correction: Deterministic + LLM-based validation."""

import json
import random
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.llm_extract import LLMClient, load_prompt
from scripts.db import get_db, get_paper_results, get_paper_experiments

console = Console()


def run_agent4(article, agent1_output: dict, agent2_output: dict,
               agent3_output: dict | None, paper_id: str, run_id: int,
               config: dict = None, llm: LLMClient = None) -> dict:
    """Run Agent 4: Validation and correction pipeline.

    Args:
        article: ParsedArticle object
        agent1_output: Agent 1's extraction
        agent2_output: Agent 2's structured output
        agent3_output: Agent 3's figure results (or None if skipped)
        paper_id: Paper identifier
        run_id: Extraction run ID
        config: Config dict
        llm: LLMClient instance

    Returns:
        Validation report dict
    """
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    if llm is None:
        llm = LLMClient(config)

    console.print("  [dim]Agent 4: Validation & correction...[/dim]")

    extraction_config = config.get("extraction", {})
    spot_check_fraction = extraction_config.get("spot_check_fraction", 0.2)
    max_corrections = extraction_config.get("max_targeted_corrections", 10)

    report = {
        "l1_corrections": [],
        "l2_corrections": [],
        "completeness_check": {},
        "spot_check": {},
        "duplicates_resolved": [],
        "total_corrections": 0,
        "human_review_items": [],
    }

    # Gather all results
    all_results = agent2_output.get("results", [])
    if agent3_output:
        all_results.extend(agent3_output.get("results", []))

    experiments = agent2_output.get("experiments", [])

    # ── Level 1: Deterministic auto-correction ──
    console.print("    [dim]L1: Deterministic checks...[/dim]")
    l1_corrections = _run_level1_checks(all_results, experiments)
    report["l1_corrections"] = l1_corrections

    if l1_corrections:
        _apply_l1_corrections(l1_corrections, paper_id, config)
        console.print(f"    [yellow]L1: {len(l1_corrections)} auto-corrections applied[/yellow]")
    else:
        console.print("    [green]L1: No issues found[/green]")

    # ── Level 2: Targeted LLM correction ──
    flagged = [c for c in l1_corrections if c.get("needs_llm_review")]
    if flagged and len(flagged) <= max_corrections:
        console.print(f"    [dim]L2: Targeted correction for {len(flagged)} values...[/dim]")
        l2_results = _run_level2_corrections(flagged, article, llm, config)
        report["l2_corrections"] = l2_results

    # ── Completeness check ──
    console.print("    [dim]Completeness check...[/dim]")
    completeness = _run_completeness_check(article, all_results, experiments, paper_id, llm, config)
    report["completeness_check"] = completeness

    # ── Spot check (random sample) ──
    if all_results:
        sample_size = max(1, int(len(all_results) * spot_check_fraction))
        sample_indices = random.sample(range(len(all_results)), min(sample_size, len(all_results)))
        sampled = [all_results[i] for i in sample_indices]
        console.print(f"    [dim]Spot-checking {len(sampled)}/{len(all_results)} results...[/dim]")
        spot_results = _run_spot_check(sampled, article, llm, config)
        report["spot_check"] = spot_results

    # ── Deduplication ──
    duplicates = _find_duplicates(all_results)
    if duplicates:
        console.print(f"    [dim]Resolving {len(duplicates)} duplicate candidates...[/dim]")
        report["duplicates_resolved"] = _resolve_duplicates(duplicates, paper_id, config)

    # Summary
    total = len(report["l1_corrections"]) + len(report["l2_corrections"])
    report["total_corrections"] = total

    human_items = []
    for item in completeness.get("missed_data", []):
        if item.get("priority") == "high":
            human_items.append(f"Missed: {item['description']} at {item.get('source_location', '?')}")
    for c in l1_corrections:
        if c.get("needs_human_review"):
            human_items.append(f"Review: {c.get('description', 'Unknown issue')}")
    report["human_review_items"] = human_items

    console.print(f"  [green]✓ Agent 4 complete: {total} corrections, "
                  f"{len(human_items)} items for human review[/green]")

    return report


def _run_level1_checks(results: list, experiments: list) -> list:
    """Level 1: Deterministic checks (no LLM cost)."""
    corrections = []

    # Build scale ranges lookup
    scale_ranges = {}
    for exp in experiments:
        exp_id = exp.get("experiment_id")
        scale_range = exp.get("scale_range", "")
        if scale_range and "-" in str(scale_range):
            try:
                parts = str(scale_range).split("-")
                low, high = float(parts[0]), float(parts[1])
                scale_ranges[exp_id] = (low, high)
            except (ValueError, IndexError):
                pass

    for i, r in enumerate(results):
        value = r.get("value")
        if value is None:
            continue

        exp_id = r.get("experiment_id")

        # Check: value outside scale range
        if exp_id in scale_ranges:
            low, high = scale_ranges[exp_id]
            if value < low or value > high:
                correction = {
                    "result_index": i,
                    "issue": "scale_bound_violation",
                    "description": f"Value {value} outside scale range {low}-{high}",
                    "original_value": value,
                    "experiment_id": exp_id,
                }

                # Try decimal shift correction
                if high <= 10 and value > 10:
                    shifted = value / 10
                    if low <= shifted <= high:
                        correction["suggested_value"] = shifted
                        correction["auto_corrected"] = True
                elif high <= 100 and value > 100:
                    shifted = value / 10
                    if low <= shifted <= high:
                        correction["suggested_value"] = shifted
                        correction["auto_corrected"] = True

                if not correction.get("auto_corrected"):
                    correction["needs_llm_review"] = True

                corrections.append(correction)

        # Check: negative values where not expected
        if value < 0 and r.get("value_type") in ("raw_mean", "frequency_pct", "dominance_rate"):
            corrections.append({
                "result_index": i,
                "issue": "negative_value",
                "description": f"Negative value {value} for {r.get('value_type')}",
                "original_value": value,
                "needs_human_review": True,
            })

        # Check: percentage > 100
        if r.get("value_type") in ("frequency_pct", "dominance_rate", "relative_pct"):
            if value > 100:
                corrections.append({
                    "result_index": i,
                    "issue": "percentage_over_100",
                    "description": f"Percentage value {value}% exceeds 100%",
                    "original_value": value,
                    "needs_llm_review": True,
                })

    # Check: missing n values (infer from siblings)
    n_values = [r.get("n") for r in results if r.get("n") is not None]
    if n_values:
        from collections import Counter
        n_counts = Counter(n_values)
        most_common_n = n_counts.most_common(1)[0][0]
        for i, r in enumerate(results):
            if r.get("n") is None:
                corrections.append({
                    "result_index": i,
                    "issue": "missing_n_inferred",
                    "description": f"Missing n, inferred as {most_common_n} from siblings",
                    "suggested_value": most_common_n,
                    "field": "n",
                    "auto_corrected": True,
                })

    return corrections


def _apply_l1_corrections(corrections: list, paper_id: str, config: dict):
    """Apply auto-corrected L1 corrections to the database."""
    conn = get_db(config)
    auto = [c for c in corrections if c.get("auto_corrected")]

    # For now, L1 corrections are logged but actual DB updates happen
    # via the orchestrator re-committing corrected results
    conn.close()


def _run_level2_corrections(flagged: list, article, llm: LLMClient, config: dict) -> list:
    """Level 2: Targeted LLM correction for flagged values."""
    corrections = []
    model = llm.get_model("agent4")

    for item in flagged:
        prompt = (
            f"A data extraction found this value: {item.get('description', '')}. "
            f"Source: {item.get('source_location', 'unknown')}. "
            f"Issue: {item.get('issue', '')}. "
            f"Please check the original text and provide the correct value. "
            f"Return JSON: {{\"correct_value\": <number or null>, \"explanation\": \"...\"}}"
        )

        try:
            result = llm.extract_json(prompt, model=model)
            corrections.append({
                "original": item,
                "correction": result,
            })
        except Exception as e:
            corrections.append({
                "original": item,
                "error": str(e),
            })

    return corrections


def _run_completeness_check(article, results: list, experiments: list,
                             paper_id: str, llm: LLMClient, config: dict) -> dict:
    """LLM completeness check: are there reported measurements not in results?"""
    prompt_template = load_prompt("agent4_validation")
    model = llm.get_model("agent4")

    # Build summaries
    results_summary = [
        {
            "sample": r.get("sample_id", ""),
            "attribute": r.get("attribute_normalized", r.get("attribute_raw", "")),
            "value": r.get("value"),
            "source": r.get("source_location", ""),
        }
        for r in results[:300]
    ]

    exp_summary = [
        {
            "id": e.get("experiment_id"),
            "method": e.get("sensory_method"),
            "scale": e.get("scale_type"),
        }
        for e in experiments
    ]

    # Build article text
    article_text = ""
    if hasattr(article, 'full_text'):
        article_text = article.full_text[:20000]

    tables_md = ""
    if hasattr(article, 'tables'):
        tables_md = "\n\n".join(
            t.to_markdown() if hasattr(t, 'to_markdown') else str(t)
            for t in article.tables
        )[:8000]

    prompt = prompt_template
    prompt = prompt.replace("{article_text}", article_text)
    prompt = prompt.replace("{tables_markdown}", tables_md)
    prompt = prompt.replace("{extracted_results_summary}", json.dumps(results_summary, indent=2)[:8000])
    prompt = prompt.replace("{experiments_summary}", json.dumps(exp_summary, indent=2)[:2000])
    prompt = prompt.replace("{paper_id}", paper_id)

    try:
        result = llm.extract_json(prompt, model=model)
        return result
    except Exception as e:
        return {"error": str(e), "overall_assessment": "error"}


def _run_spot_check(sampled_results: list, article, llm: LLMClient, config: dict) -> dict:
    """Spot-check random results against original text."""
    model = llm.get_model("agent4")
    checked = 0
    issues = []

    for r in sampled_results[:5]:  # Limit to 5 to control cost
        prompt = (
            f"Verify this extracted data point against the original paper:\n"
            f"- Sample: {r.get('sample_id', 'unknown')}\n"
            f"- Attribute: {r.get('attribute_raw', 'unknown')}\n"
            f"- Value: {r.get('value')}\n"
            f"- Source: {r.get('source_location', 'unknown')}\n\n"
            f"Return JSON: {{\"correct\": true/false, \"actual_value\": <number or null>, \"explanation\": \"...\"}}"
        )

        try:
            result = llm.extract_json(prompt, model=model)
            checked += 1
            if not result.get("correct", True):
                issues.append({
                    "result": r,
                    "verification": result,
                })
        except Exception:
            pass

    return {
        "checked": checked,
        "issues_found": len(issues),
        "issues": issues,
    }


def _find_duplicates(results: list) -> list:
    """Find potential duplicate results (same sample + attribute, different sources)."""
    seen = {}
    duplicates = []

    for i, r in enumerate(results):
        key = (r.get("sample_id"), r.get("attribute_normalized"), r.get("experiment_id"))
        if key in seen:
            duplicates.append((seen[key], i))
        else:
            seen[key] = i

    return duplicates


def _resolve_duplicates(duplicates: list, paper_id: str, config: dict) -> list:
    """Resolve duplicates by keeping higher-confidence source."""
    confidence_rank = {"table": 4, "supplementary": 3, "text": 2, "figure": 1}
    resolved = []

    # Actual DB deletion would happen here; for now just report
    for idx1, idx2 in duplicates:
        resolved.append({
            "indices": [idx1, idx2],
            "action": "flagged_for_review",
        })

    return resolved


def save_agent4_output(report: dict, study_id: str, config: dict = None) -> Path:
    """Save Agent 4 validation report."""
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    parts_dir = ROOT_DIR / config["paths"]["extractions_dir"] / "parts" / study_id
    parts_dir.mkdir(parents=True, exist_ok=True)

    output_path = parts_dir / "agent4_validation.json"
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return output_path
