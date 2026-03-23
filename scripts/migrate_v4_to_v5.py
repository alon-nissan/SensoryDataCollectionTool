#!/usr/bin/env python3
"""Migrate v4 (10-table) database to v5 (7-table) schema.

Denormalizes stimuli + samples + sample_components + results → observations.
Infrastructure tables (substances, substance_aliases, extraction_runs, unit_conversions)
are copied as-is. Peripheral data from old papers/experiments columns is saved as
context.json files per paper.

Usage:
    python scripts/migrate_v4_to_v5.py                      # default paths
    python scripts/migrate_v4_to_v5.py --v4 data/old.db     # custom source
    python scripts/migrate_v4_to_v5.py --dry-run             # preview only
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.init_db import init_database

console = Console()


def load_config() -> dict:
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def migrate(v4_path: Path, v5_path: Path, config: dict, dry_run: bool = False):
    """Run the full v4 → v5 migration."""
    console.print(f"[bold]Migrating:[/] {v4_path} → {v5_path}")

    if not v4_path.exists():
        console.print(f"[red]v4 database not found: {v4_path}[/red]")
        sys.exit(1)

    # Open v4
    v4 = sqlite3.connect(str(v4_path))
    v4.row_factory = sqlite3.Row
    v4.execute("PRAGMA foreign_keys = OFF")  # avoid FK issues during read

    # Create fresh v5
    if v5_path.exists() and not dry_run:
        v5_path.unlink()
    v5 = init_database(v5_path) if not dry_run else None

    # ── 1. Migrate papers (slim down) ─────────────────────────────────
    console.print("\n[cyan]1. Papers[/cyan]")
    papers = v4.execute("SELECT * FROM papers").fetchall()
    peripheral_data = {}  # paper_id → context dict

    for p in papers:
        paper_id = p["paper_id"]

        # Extract peripheral fields for context.json
        peripheral = {}
        for col in ("country", "food_category", "num_experiments", "panel_types",
                     "max_panel_size", "has_figure_data", "has_supplementary_data",
                     "data_availability", "data_availability_details", "context_json"):
            val = p[col] if col in p.keys() else None
            if val is not None:
                if col == "context_json" and isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except json.JSONDecodeError:
                        pass
                peripheral[col] = val
        if peripheral:
            peripheral_data[paper_id] = peripheral

        if not dry_run:
            # Insert without latest_run_id first (FK to extraction_runs)
            v5.execute(
                "INSERT INTO papers (paper_id, doi, title, year, journal, validation_status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (paper_id, p["doi"], p["title"], p["year"], p["journal"],
                 p["validation_status"]),
            )

    console.print(f"  {len(papers)} papers")

    # ── 2. Migrate experiments (slim down) ────────────────────────────
    console.print("[cyan]2. Experiments[/cyan]")
    experiments = v4.execute("SELECT * FROM experiments").fetchall()

    for e in experiments:
        exp_id = e["experiment_id"]
        paper_id = e["paper_id"]

        # Save panel/design info to peripheral context
        exp_peripheral = {}
        for col in ("panel_size", "panel_type", "serving_temp_c",
                     "serving_temp_raw", "conditions_json"):
            val = e[col] if col in e.keys() else None
            if val is not None:
                if col == "conditions_json" and isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except json.JSONDecodeError:
                        pass
                exp_peripheral[col] = val

        if exp_peripheral:
            ctx = peripheral_data.setdefault(paper_id, {})
            exps = ctx.setdefault("experiments", {})
            exps[exp_id] = exp_peripheral

        if not dry_run:
            v5.execute(
                "INSERT INTO experiments (experiment_id, paper_id, experiment_label, "
                "sensory_method, scale_type, scale_range) VALUES (?, ?, ?, ?, ?, ?)",
                (exp_id, paper_id, e["experiment_label"], e["sensory_method"],
                 e["scale_type"], e["scale_range"]),
            )

    console.print(f"  {len(experiments)} experiments")

    # ── 3. Copy infrastructure tables ─────────────────────────────────
    console.print("[cyan]3. Infrastructure tables[/cyan]")

    # Substances
    substances = v4.execute("SELECT * FROM substances").fetchall()
    if not dry_run:
        for s in substances:
            v5.execute(
                "INSERT INTO substances (substance_id, normalized_name, cas_number, "
                "smiles, molecular_weight, category, properties_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (s["substance_id"], s["normalized_name"], s["cas_number"],
                 s["smiles"], s["molecular_weight"], s["category"], s["properties_json"]),
            )
    console.print(f"  {len(substances)} substances")

    # Substance aliases
    aliases = v4.execute("SELECT * FROM substance_aliases").fetchall()
    if not dry_run:
        for a in aliases:
            v5.execute(
                "INSERT OR IGNORE INTO substance_aliases (alias, substance_id) VALUES (?, ?)",
                (a["alias"], a["substance_id"]),
            )
    console.print(f"  {len(aliases)} substance aliases")

    # Extraction runs
    runs = v4.execute("SELECT * FROM extraction_runs").fetchall()
    if not dry_run:
        for r in runs:
            v5.execute(
                "INSERT INTO extraction_runs (run_id, paper_id, run_timestamp, "
                "agent1_prompt_version, agent2_prompt_version, agent3_prompt_version, "
                "agent4_prompt_version, model_versions, status, validation_report, "
                "corrections_applied, human_review_items, token_usage, total_cost_usd, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["run_id"], r["paper_id"], r["run_timestamp"],
                 r["agent1_prompt_version"], r["agent2_prompt_version"],
                 r["agent3_prompt_version"], r["agent4_prompt_version"],
                 r["model_versions"], r["status"], r["validation_report"],
                 r["corrections_applied"], r["human_review_items"],
                 r["token_usage"], r["total_cost_usd"], r["notes"]),
            )
    console.print(f"  {len(runs)} extraction runs")

    # Now update papers with latest_run_id (FK satisfied after runs are inserted)
    if not dry_run:
        for p in papers:
            if p["latest_run_id"] is not None:
                v5.execute(
                    "UPDATE papers SET latest_run_id = ? WHERE paper_id = ?",
                    (p["latest_run_id"], p["paper_id"]),
                )

    # ── 4. Denormalize results → observations ─────────────────────────
    console.print("[cyan]4. Denormalize results → observations[/cyan]")

    # Build substance sourcing from stimuli table
    stimuli = v4.execute("SELECT * FROM stimuli").fetchall()
    stimulus_sourcing = {}  # paper_id → {substance_name → sourcing_info}
    stimulus_lookup = {}    # stimulus_id → (substance_name, paper_id)
    for st in stimuli:
        sub_row = v4.execute(
            "SELECT normalized_name FROM substances WHERE substance_id = ?",
            (st["substance_id"],),
        ).fetchone()
        substance_name = sub_row["normalized_name"] if sub_row else st["original_name"]
        stimulus_lookup[st["stimulus_id"]] = (substance_name, st["paper_id"])

        sourcing = {}
        for col in ("supplier", "purity", "form", "details_json"):
            val = st[col] if col in st.keys() else None
            if val is not None:
                if col == "details_json" and isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except json.JSONDecodeError:
                        pass
                sourcing[col] = val
        if sourcing:
            ctx = peripheral_data.setdefault(st["paper_id"], {})
            sub_sourcing = ctx.setdefault("substance_sourcing", {})
            sub_sourcing[substance_name] = sourcing

    console.print(f"  {len(stimuli)} stimuli mapped")

    # Build sample → components lookup
    sample_components = {}  # sample_id → [{substance, concentration, unit}, ...]
    sample_primary = {}     # sample_id → (substance_name, concentration, unit)
    for sc in v4.execute("SELECT * FROM sample_components").fetchall():
        sid = sc["sample_id"]
        stim_id = sc["stimulus_id"]
        substance_name, _ = stimulus_lookup.get(stim_id, ("unknown", ""))
        comp = {
            "substance": substance_name,
            "concentration": sc["concentration"],
            "unit": sc["unit"],
        }
        sample_components.setdefault(sid, []).append(comp)
        # First component is primary (for substance_name/concentration columns)
        if sid not in sample_primary:
            sample_primary[sid] = (substance_name, sc["concentration"], sc["unit"])

    # Migrate results
    results = v4.execute("SELECT * FROM results ORDER BY result_id").fetchall()
    obs_count = 0
    seen_obs = set()  # dedup key for mixture rows

    for r in results:
        sample_id = r["sample_id"]

        # Get sample info
        base_matrix = None
        is_control = 0
        substance_name = None
        components_json = None

        if sample_id:
            sample_row = v4.execute(
                "SELECT * FROM samples WHERE sample_id = ?", (sample_id,)
            ).fetchone()
            if sample_row:
                base_matrix = sample_row["base_matrix"]
                is_control = sample_row["is_control"]

            comps = sample_components.get(sample_id, [])
            if comps:
                components_json = json.dumps(comps)
                primary = sample_primary.get(sample_id)
                if primary:
                    substance_name = primary[0]  # name only, concentration lives in components_json

            # Dedup: mixture samples produce multiple join rows in v4 queries,
            # but here we read from results directly — one row per result_id.
            # However, we still need to dedup if the same result was inserted
            # multiple times.
            dedup_key = (r["result_id"],)
            if dedup_key in seen_obs:
                continue
            seen_obs.add(dedup_key)

        if not dry_run:
            v5.execute(
                "INSERT INTO observations (paper_id, experiment_id, substance_name, "
                "components_json, base_matrix, "
                "is_control, attribute_raw, attribute_normalized, "
                "value, value_type, error_value, error_type, "
                "n, source_type, source_location, extraction_confidence, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["paper_id"], r["experiment_id"], substance_name,
                 components_json, base_matrix, is_control,
                 r["attribute_raw"], r["attribute_normalized"],
                 r["value"], r["value_type"],
                 r["error_value"], r["error_type"], r["n"],
                 r["source_type"], r["source_location"],
                 r["extraction_confidence"], r["run_id"]),
            )
        obs_count += 1

    console.print(f"  {len(results)} results → {obs_count} observations")

    # ── 5. Save peripheral context JSON files ─────────────────────────
    console.print("[cyan]5. Peripheral context[/cyan]")
    extractions_dir = ROOT_DIR / config["paths"]["extractions_dir"]

    for paper_id, context in peripheral_data.items():
        parts_dir = extractions_dir / "parts" / paper_id
        parts_dir.mkdir(parents=True, exist_ok=True)
        context_path = parts_dir / "context.json"
        if not dry_run:
            with open(context_path, "w") as f:
                json.dump(context, f, indent=2, ensure_ascii=False)
        console.print(f"  Saved context for {paper_id}")

    # ── 6. Commit and verify ──────────────────────────────────────────
    if not dry_run:
        v5.commit()

        # Verify counts
        console.print("\n[bold green]Verification:[/bold green]")
        for table in ("papers", "experiments", "observations", "substances",
                       "substance_aliases", "extraction_runs", "unit_conversions"):
            count = v5.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            console.print(f"  {table}: {count}")

        # Spot check: random observations
        console.print("\n[bold]Spot check (5 random observations):[/bold]")
        rows = v5.execute(
            "SELECT substance_name, concentration, concentration_unit, "
            "attribute_normalized, value, value_type, source_type "
            "FROM observations ORDER BY RANDOM() LIMIT 5"
        ).fetchall()
        for row in rows:
            console.print(
                f"  {row[0] or '(null)'} @ {row[1]} {row[2] or ''} → "
                f"{row[3]}: {row[4]} ({row[5]}, {row[6]})"
            )

        v5.close()

    v4.close()
    console.print(f"\n[bold green]Migration {'preview' if dry_run else 'complete'}.[/bold green]")


def main():
    parser = argparse.ArgumentParser(description="Migrate v4 → v5 sensory data schema")
    parser.add_argument("--v4", type=Path, default=None,
                        help="Path to v4 database (default: data/sensory_data_v4_backup.db)")
    parser.add_argument("--v5", type=Path, default=None,
                        help="Path for v5 database (default: data/sensory_data.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview migration without writing")
    args = parser.parse_args()

    config = load_config()

    v4_path = args.v4 or (ROOT_DIR / "data" / "sensory_data_v4_backup.db")
    v5_path = args.v5 or (ROOT_DIR / config["paths"]["sqlite_db"])

    migrate(v4_path, v5_path, config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
