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

from scripts.llm_extract import LLMClient, PromptTooLargeError, load_prompt
from scripts.db import get_db, get_paper_observations, get_paper_experiments, get_panels_for_paper

console = Console()


def run_agent4(article, agent1_output: dict, agent2_output: dict,
               agent3_output: dict | None, paper_id: str, run_id: int,
               config: dict = None, llm: LLMClient = None,
               figure_metadata: list = None) -> dict:
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
        figure_metadata: List of figure dicts with local_path for visual verification

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

    # Gather all observations from agents
    all_observations = agent2_output.get("observations", [])
    if agent3_output:
        all_observations = all_observations + agent3_output.get("observations", [])

    experiments = agent2_output.get("experiments", [])
    panels = agent2_output.get("panels", [])

    # ── Level 1: Deterministic auto-correction ──
    console.print("    [dim]L1: Deterministic checks...[/dim]")
    l1_corrections = _run_level1_checks(all_observations, experiments)
    # Panel-specific checks
    panel_issues = _run_panel_checks(all_observations, panels, paper_id, config)
    l1_corrections.extend(panel_issues)
    if panel_issues:
        console.print(f"    [yellow]Panel checks: {len(panel_issues)} issues found[/yellow]")
    report["l1_corrections"] = l1_corrections

    if l1_corrections:
        applied = _apply_l1_corrections(l1_corrections, paper_id, config)
        console.print(f"    [yellow]L1: {len(l1_corrections)} issues found, {applied} auto-corrections applied[/yellow]")
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
    completeness = _run_completeness_check(article, all_observations, experiments, paper_id, llm, config)
    report["completeness_check"] = completeness

    # ── Spot check (random sample) ──
    if all_observations:
        sample_size = max(1, int(len(all_observations) * spot_check_fraction))
        sample_indices = random.sample(range(len(all_observations)), min(sample_size, len(all_observations)))
        sampled = [all_observations[i] for i in sample_indices]
        console.print(f"    [dim]Spot-checking {len(sampled)}/{len(all_observations)} observations...[/dim]")
        spot_results = _run_spot_check(sampled, article, llm, config,
                                       figure_metadata=figure_metadata)
        report["spot_check"] = spot_results

    # ── Deduplication ──
    duplicates = _find_duplicates(all_observations)
    if duplicates:
        console.print(f"    [dim]Resolving {len(duplicates)} duplicate candidates...[/dim]")
        report["duplicates_resolved"] = _resolve_duplicates(duplicates, all_observations, paper_id, config)

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


def _run_level1_checks(observations: list, experiments: list) -> list:
    """Level 1: Deterministic checks (no LLM cost)."""
    corrections = []

    # Build scale ranges lookup
    scale_ranges = {}
    for exp in experiments:
        exp_label = exp.get("experiment", "")
        scale_range = exp.get("scale_range", "")
        if scale_range and "-" in str(scale_range):
            try:
                parts = str(scale_range).split("-")
                low, high = float(parts[0]), float(parts[1])
                scale_ranges[exp_label] = (low, high)
            except (ValueError, IndexError):
                pass

    for i, obs in enumerate(observations):
        value = obs.get("value")
        if value is None:
            continue

        exp_label = obs.get("experiment", "")
        value_type = obs.get("value_type", "")

        # Derived params with null concentration/sample_label are valid
        if value_type == "derived_param":
            continue

        # Check: value outside scale range
        if exp_label in scale_ranges:
            low, high = scale_ranges[exp_label]
            if value < low or value > high:
                correction = {
                    "observation_index": i,
                    "issue": "scale_bound_violation",
                    "description": f"Value {value} outside scale range {low}-{high}",
                    "original_value": value,
                    "substance": obs.get("substance"),
                    "attribute": obs.get("attribute"),
                    "experiment": exp_label,
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
        if value < 0 and value_type in ("raw_mean", "frequency_pct", "dominance_rate"):
            corrections.append({
                "observation_index": i,
                "issue": "negative_value",
                "description": f"Negative value {value} for {value_type}",
                "original_value": value,
                "needs_human_review": True,
            })

        # Check: percentage > 100
        if value_type in ("frequency_pct", "dominance_rate", "relative_pct"):
            if value > 100:
                corrections.append({
                    "observation_index": i,
                    "issue": "percentage_over_100",
                    "description": f"Percentage value {value}% exceeds 100%",
                    "original_value": value,
                    "needs_llm_review": True,
                })

    return corrections


def _apply_l1_corrections(corrections: list, paper_id: str, config: dict) -> int:
    """Apply auto-corrected L1 corrections to the database. Returns count applied."""
    conn = get_db(config)
    auto = [c for c in corrections if c.get("auto_corrected")]
    applied = 0

    # Get observation_ids for this paper to map indices
    db_observations = get_paper_observations(conn, paper_id)

    for correction in auto:
        idx = correction.get("observation_index")
        if idx is None or idx >= len(db_observations):
            continue

        obs_id = db_observations[idx].get("observation_id")
        if obs_id is None:
            continue

        field = correction.get("field", "value")
        suggested = correction.get("suggested_value")
        if suggested is None:
            continue

        try:
            conn.execute(
                f"UPDATE observations SET {field} = ? WHERE observation_id = ?",
                (suggested, obs_id),
            )
            applied += 1
        except Exception as e:
            console.print(f"    [dim]L1 correction failed for obs {obs_id}: {e}[/dim]")

    if applied:
        conn.commit()
    conn.close()
    return applied


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


def _run_completeness_check(article, observations: list, experiments: list,
                             paper_id: str, llm: LLMClient, config: dict) -> dict:
    """LLM completeness check: are there reported measurements not in observations?"""
    prompt_template = load_prompt("agent4_validation")
    model = llm.get_model("agent4")

    # Build summaries
    obs_summary = []
    for obs in observations:
        comps = obs.get("components") or []
        conc = comps[0].get("concentration") if comps else None
        obs_summary.append({
            "substance": obs.get("substance", obs.get("substance_name", "")),
            "concentration": conc,
            "attribute": obs.get("attribute_normalized", obs.get("attribute", "")),
            "value": obs.get("value"),
            "value_type": obs.get("value_type", ""),
            "source": obs.get("source", obs.get("source_location", "")),
        })

    exp_summary = [
        {
            "experiment": e.get("experiment"),
            "method": e.get("method", e.get("sensory_method")),
            "scale": e.get("scale_type"),
        }
        for e in experiments
    ]

    # Build article text
    article_text = ""
    if hasattr(article, 'full_text'):
        article_text = article.full_text

    tables_md = ""
    if hasattr(article, 'tables'):
        tables_md = "\n\n".join(
            t.to_markdown() if hasattr(t, 'to_markdown') else str(t)
            for t in article.tables
        )

    prompt = prompt_template
    prompt = prompt.replace("{article_text}", article_text)
    prompt = prompt.replace("{tables_markdown}", tables_md)
    prompt = prompt.replace("{extracted_results_summary}", json.dumps(obs_summary, indent=2))
    prompt = prompt.replace("{experiments_summary}", json.dumps(exp_summary, indent=2))
    prompt = prompt.replace("{paper_id}", paper_id)

    try:
        result = llm.extract_json(prompt, model=model, agent="agent4_completeness")
        return result
    except PromptTooLargeError as e:
        return {"error": str(e), "overall_assessment": "skipped_too_large"}
    except Exception as e:
        return {"error": str(e), "overall_assessment": "error"}


def _run_spot_check(sampled_observations: list, article, llm: LLMClient, config: dict,
                    figure_metadata: list = None) -> dict:
    """Spot-check random observations against original text or figure images."""
    model = llm.get_model("agent4")
    checked = 0
    issues = []

    # Build figure path lookup: figure_id → local_path
    figure_paths = {}
    if figure_metadata:
        for fig in figure_metadata:
            fig_id = fig.get("figure_id", "")
            local_path = fig.get("local_path", "")
            if fig_id and local_path and Path(local_path).exists():
                figure_paths[fig_id] = local_path

    max_spot = config.get("extraction", {}).get("max_spot_check_observations", 5)
    for obs in sampled_observations[:max_spot]:
        source_location = obs.get("source", obs.get("source_location", "unknown"))
        source_type = obs.get("source_type", "")

        # Extract concentration from components
        spot_components = obs.get("components") or []
        spot_conc = spot_components[0].get("concentration") if spot_components else "N/A"

        prompt = (
            f"Verify this extracted data point against the original paper:\n"
            f"- Substance: {obs.get('substance', obs.get('substance_name', 'unknown'))}\n"
            f"- Concentration: {spot_conc}\n"
            f"- Attribute: {obs.get('attribute', obs.get('attribute_raw', 'unknown'))}\n"
            f"- Value: {obs.get('value')}\n"
            f"- Source: {source_location}\n\n"
            f"Return JSON: {{\"correct\": true/false, \"actual_value\": <number or null>, \"explanation\": \"...\"}}"
        )

        try:
            # If observation is from a figure and we have the image, use vision
            fig_path = None
            if source_type == "figure":
                for fig_id, path in figure_paths.items():
                    if fig_id.lower() in source_location.lower():
                        fig_path = path
                        break

            if fig_path:
                result = llm.extract_json_with_image(prompt, fig_path, model=model)
            else:
                result = llm.extract_json(prompt, model=model)

            checked += 1
            if not result.get("correct", True):
                issues.append({
                    "observation": obs,
                    "verification": result,
                })
        except Exception as e:
            issues.append({
                "observation": obs,
                "error": str(e),
            })

    return {
        "checked": checked,
        "issues_found": len(issues),
        "issues": issues,
    }


_DEMOGRAPHIC_KEYWORDS = {
    "mean_age", "age", "age_mean", "mean age", "bmi", "body mass index",
    "gender", "sex", "female", "male", "percentage female", "percent female",
    "gender ratio", "sex ratio", "taster status", "genotype",
    "height", "weight", "number of panelists", "panel size",
}


def _run_panel_checks(observations: list, panels: list, paper_id: str, config: dict) -> list:
    """Panel-specific deterministic checks.

    1. Demographic contamination: observations whose attribute looks like a panel demographic
    2. Panel FK: observations referencing a panel_label that wasn't created
    3. Subgroup size: subgroup panel_size must be ≤ parent panel_size
    """
    issues = []
    panel_labels = {p.get("panel_label") for p in panels}

    for i, obs in enumerate(observations):
        attr_raw = (obs.get("attribute") or obs.get("attribute_raw") or "").lower().strip()

        # 1. Demographic contamination check
        if any(kw in attr_raw for kw in _DEMOGRAPHIC_KEYWORDS):
            issues.append({
                "observation_index": i,
                "issue": "demographic_as_observation",
                "description": (
                    f"Attribute '{attr_raw}' looks like a panel demographic, not a sensory measurement. "
                    "Panel attributes (age, BMI, gender, PROP status) must be stored in the panels table."
                ),
                "substance": obs.get("substance", obs.get("substance_name")),
                "attribute": attr_raw,
                "needs_human_review": True,
            })

        # 2. Panel FK check
        panel_label = obs.get("panel_label")
        if panel_label and panel_labels and panel_label not in panel_labels:
            issues.append({
                "observation_index": i,
                "issue": "invalid_panel_label",
                "description": (
                    f"Observation references panel_label '{panel_label}' which is not in the panels list."
                ),
                "substance": obs.get("substance", obs.get("substance_name")),
                "attribute": attr_raw,
                "needs_human_review": True,
            })

    # 3. Subgroup size check (from DB panels if available)
    try:
        conn = get_db(config)
        db_panels = get_panels_for_paper(conn, paper_id)
        conn.close()
        panel_size_map = {p["panel_id"]: p.get("panel_size") for p in db_panels}
        for panel in db_panels:
            parent_id = panel.get("parent_panel_id")
            size = panel.get("panel_size")
            if parent_id and size is not None:
                parent_size = panel_size_map.get(parent_id)
                if parent_size is not None and size > parent_size:
                    issues.append({
                        "issue": "subgroup_size_exceeds_parent",
                        "description": (
                            f"Subgroup panel '{panel['panel_id']}' (n={size}) is larger than "
                            f"parent panel '{parent_id}' (n={parent_size})."
                        ),
                        "needs_human_review": True,
                    })
    except Exception:
        pass  # DB may not exist yet during pipeline runs

    return issues


def _find_duplicates(observations: list) -> list:
    """Find potential duplicate observations (same substance+concentration+attribute, different sources)."""
    seen = {}
    duplicates = []

    for i, obs in enumerate(observations):
        # Extract concentration from components for dedup key
        components = obs.get("components") or []
        concentration = components[0].get("concentration") if components else None

        # For derived params, dedup by (substance, attribute, experiment) — no concentration
        if obs.get("value_type") == "derived_param":
            key = (
                obs.get("substance", obs.get("substance_name")),
                obs.get("attribute_normalized", obs.get("attribute")),
                obs.get("experiment"),
            )
        else:
            key = (
                obs.get("substance", obs.get("substance_name")),
                concentration,
                obs.get("base_matrix"),
                obs.get("attribute_normalized", obs.get("attribute")),
                obs.get("experiment"),
            )

        if key in seen:
            duplicates.append((seen[key], i))
        else:
            seen[key] = i

    return duplicates


def _resolve_duplicates(duplicates: list, observations: list,
                        paper_id: str, config: dict) -> list:
    """Resolve duplicates by keeping higher-confidence source. Deletes losers from DB."""
    confidence_rank = {"table": 4, "supplementary": 3, "text": 2, "figure": 1}
    resolved = []

    conn = get_db(config)
    db_observations = get_paper_observations(conn, paper_id)
    deleted = 0

    for idx1, idx2 in duplicates:
        obs1 = observations[idx1]
        obs2 = observations[idx2]

        src1 = obs1.get("source_type", "")
        src2 = obs2.get("source_type", "")
        rank1 = confidence_rank.get(src1, 0)
        rank2 = confidence_rank.get(src2, 0)

        # Determine which to keep (higher rank wins; if tied, keep first)
        if rank2 > rank1:
            keep_idx, drop_idx = idx2, idx1
        else:
            keep_idx, drop_idx = idx1, idx2

        action = "deleted_lower_confidence"

        # Try to delete the lower-confidence observation from DB
        if drop_idx < len(db_observations):
            drop_obs_id = db_observations[drop_idx].get("observation_id")
            if drop_obs_id is not None:
                try:
                    conn.execute(
                        "DELETE FROM observations WHERE observation_id = ?",
                        (drop_obs_id,),
                    )
                    deleted += 1
                except Exception as e:
                    action = f"deletion_failed: {e}"

        resolved.append({
            "kept_index": keep_idx,
            "dropped_index": drop_idx,
            "kept_source": observations[keep_idx].get("source_type"),
            "dropped_source": observations[drop_idx].get("source_type"),
            "action": action,
        })

    if deleted:
        conn.commit()
        console.print(f"    [cyan]Deleted {deleted} duplicate observations[/cyan]")
    conn.close()

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
