#!/usr/bin/env python3
"""Database access layer for the v4 sensory data schema."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def get_db_path(config: dict = None) -> Path:
    """Get the database file path from config."""
    if config is None:
        config = load_config()
    return ROOT_DIR / config["paths"]["sqlite_db"]


def get_db(config: dict = None) -> sqlite3.Connection:
    """Get a database connection with proper settings."""
    db_path = get_db_path(config)
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database not found at {db_path}. Run: python scripts/init_db.py"
        )
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Insert helpers ───────────────────────────────────────────

def insert_paper(conn: sqlite3.Connection, paper: dict) -> str:
    """Insert or update a paper record. Returns paper_id."""
    cols = [
        "paper_id", "doi", "title", "year", "journal", "country",
        "food_category", "num_experiments", "panel_types", "max_panel_size",
        "has_figure_data", "has_supplementary_data", "data_availability",
        "data_availability_details", "validation_status", "context_json",
    ]
    values = {c: paper.get(c) for c in cols}
    if isinstance(values.get("context_json"), dict):
        values["context_json"] = json.dumps(values["context_json"])
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    update_set = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "paper_id")
    
    conn.execute(
        f"INSERT INTO papers ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(paper_id) DO UPDATE SET {update_set}",
        values,
    )
    conn.commit()
    return values["paper_id"]


def insert_experiment(conn: sqlite3.Connection, experiment: dict) -> str:
    """Insert an experiment record. Returns experiment_id."""
    cols = [
        "experiment_id", "paper_id", "experiment_label", "sensory_method",
        "scale_type", "scale_range", "panel_size", "panel_type",
        "serving_temp_c", "serving_temp_raw", "conditions_json",
    ]
    values = {c: experiment.get(c) for c in cols}
    if isinstance(values.get("conditions_json"), dict):
        values["conditions_json"] = json.dumps(values["conditions_json"])
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    
    conn.execute(
        f"INSERT OR REPLACE INTO experiments ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return values["experiment_id"]


def insert_substance(conn: sqlite3.Connection, substance: dict) -> int:
    """Insert a substance. Returns substance_id (auto-increment)."""
    cols = [
        "normalized_name", "cas_number", "smiles", "molecular_weight",
        "category", "properties_json",
    ]
    values = {c: substance.get(c) for c in cols}
    if isinstance(values.get("properties_json"), dict):
        values["properties_json"] = json.dumps(values["properties_json"])
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    
    cursor = conn.execute(
        f"INSERT INTO substances ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cursor.lastrowid


def add_substance_alias(conn: sqlite3.Connection, alias: str, substance_id: int):
    """Add a name alias for a substance."""
    conn.execute(
        "INSERT OR IGNORE INTO substance_aliases (alias, substance_id) VALUES (?, ?)",
        (alias.lower().strip(), substance_id),
    )
    conn.commit()


def insert_stimulus(conn: sqlite3.Connection, stimulus: dict) -> str:
    """Insert a paper-specific stimulus. Returns stimulus_id."""
    cols = [
        "stimulus_id", "paper_id", "substance_id", "original_name",
        "supplier", "purity", "form", "details_json",
    ]
    values = {c: stimulus.get(c) for c in cols}
    if isinstance(values.get("details_json"), dict):
        values["details_json"] = json.dumps(values["details_json"])
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    
    conn.execute(
        f"INSERT OR REPLACE INTO stimuli ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return values["stimulus_id"]


def insert_sample(conn: sqlite3.Connection, sample: dict) -> str:
    """Insert a sample. Returns sample_id."""
    cols = [
        "sample_id", "paper_id", "experiment_id", "sample_label",
        "base_matrix", "is_control",
    ]
    values = {c: sample.get(c) for c in cols}
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    
    conn.execute(
        f"INSERT OR REPLACE INTO samples ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return values["sample_id"]


def insert_sample_component(conn: sqlite3.Connection, component: dict):
    """Insert a sample-stimulus component link with concentration."""
    cols = [
        "sample_id", "stimulus_id", "concentration", "unit",
        "concentration_canonical", "unit_canonical",
    ]
    values = {c: component.get(c) for c in cols}
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    
    conn.execute(
        f"INSERT OR REPLACE INTO sample_components ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def insert_result(conn: sqlite3.Connection, result: dict) -> int:
    """Insert a result row. Returns result_id."""
    cols = [
        "paper_id", "experiment_id", "sample_id", "attribute_raw",
        "attribute_normalized", "attribute_category", "value", "value_type",
        "error_value", "error_type", "n", "source_type", "source_location",
        "extraction_confidence", "run_id", "context_json",
    ]
    values = {c: result.get(c) for c in cols}
    if isinstance(values.get("context_json"), dict):
        values["context_json"] = json.dumps(values["context_json"])
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    
    cursor = conn.execute(
        f"INSERT INTO results ({col_names}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cursor.lastrowid


def insert_results_batch(conn: sqlite3.Connection, results: list[dict]) -> int:
    """Insert multiple result rows efficiently. Returns count inserted."""
    if not results:
        return 0
    
    cols = [
        "paper_id", "experiment_id", "sample_id", "attribute_raw",
        "attribute_normalized", "attribute_category", "value", "value_type",
        "error_value", "error_type", "n", "source_type", "source_location",
        "extraction_confidence", "run_id", "context_json",
    ]
    
    rows = []
    for r in results:
        values = {c: r.get(c) for c in cols}
        if isinstance(values.get("context_json"), dict):
            values["context_json"] = json.dumps(values["context_json"])
        rows.append(values)
    
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    
    conn.executemany(
        f"INSERT INTO results ({col_names}) VALUES ({placeholders})",
        rows,
    )
    conn.commit()
    return len(rows)


def create_extraction_run(conn: sqlite3.Connection, paper_id: str,
                          model_versions: dict = None,
                          prompt_versions: dict = None) -> int:
    """Create a new extraction run record. Returns run_id."""
    now = datetime.now(timezone.utc).isoformat()
    pv = prompt_versions or {}
    cursor = conn.execute(
        """INSERT INTO extraction_runs 
           (paper_id, run_timestamp, agent1_prompt_version, agent2_prompt_version,
            agent3_prompt_version, agent4_prompt_version, model_versions, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress')""",
        (
            paper_id, now,
            pv.get("agent1"), pv.get("agent2"),
            pv.get("agent3"), pv.get("agent4"),
            json.dumps(model_versions or {}),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def update_extraction_run(conn: sqlite3.Connection, run_id: int, **kwargs):
    """Update an extraction run with results."""
    allowed = {
        "status", "validation_report", "corrections_applied",
        "human_review_items", "token_usage", "total_cost_usd", "notes",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    
    for k in ("validation_report", "token_usage"):
        if k in updates and isinstance(updates[k], dict):
            updates[k] = json.dumps(updates[k])
    
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["run_id"] = run_id
    
    conn.execute(
        f"UPDATE extraction_runs SET {set_clause} WHERE run_id = :run_id",
        updates,
    )
    conn.commit()


def update_paper_latest_run(conn: sqlite3.Connection, paper_id: str, run_id: int):
    """Update the latest_run_id for a paper."""
    conn.execute(
        "UPDATE papers SET latest_run_id = ? WHERE paper_id = ?",
        (run_id, paper_id),
    )
    conn.commit()


# ── Substance resolution ─────────────────────────────────────

def resolve_substance_by_alias(conn: sqlite3.Connection, name: str) -> int | None:
    """Look up a substance by alias. Returns substance_id or None."""
    row = conn.execute(
        "SELECT substance_id FROM substance_aliases WHERE alias = ?",
        (name.lower().strip(),),
    ).fetchone()
    return row["substance_id"] if row else None


def resolve_substance_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    """Look up a substance by normalized_name. Returns substance_id or None."""
    row = conn.execute(
        "SELECT substance_id FROM substances WHERE normalized_name = ?",
        (name.lower().strip(),),
    ).fetchone()
    return row["substance_id"] if row else None


def resolve_substance_by_cas(conn: sqlite3.Connection, cas: str) -> int | None:
    """Look up a substance by CAS number. Returns substance_id or None."""
    if not cas:
        return None
    row = conn.execute(
        "SELECT substance_id FROM substances WHERE cas_number = ?",
        (cas.strip(),),
    ).fetchone()
    return row["substance_id"] if row else None


def get_substance_aliases_subset(conn: sqlite3.Connection, 
                                  substance_names: list[str]) -> dict:
    """Get relevant aliases for a set of substance names.
    
    Returns dict mapping alias → normalized_name for substance names
    that appear in the provided list (useful for passing to Agent 2).
    """
    if not substance_names:
        return {}
    
    placeholders = ", ".join("?" * len(substance_names))
    rows = conn.execute(
        f"""SELECT sa.alias, s.normalized_name
            FROM substance_aliases sa
            JOIN substances s ON sa.substance_id = s.substance_id
            WHERE s.normalized_name IN ({placeholders})""",
        [n.lower().strip() for n in substance_names],
    ).fetchall()
    
    return {row["alias"]: row["normalized_name"] for row in rows}


def get_all_substance_aliases(conn: sqlite3.Connection) -> dict:
    """Get all aliases. Returns dict mapping alias → normalized_name."""
    rows = conn.execute(
        """SELECT sa.alias, s.normalized_name
           FROM substance_aliases sa
           JOIN substances s ON sa.substance_id = s.substance_id"""
    ).fetchall()
    return {row["alias"]: row["normalized_name"] for row in rows}


# ── Unit conversion ──────────────────────────────────────────

def get_unit_conversion(conn: sqlite3.Connection, unit_raw: str, 
                        unit_canonical: str) -> float | None:
    """Look up a unit conversion multiplier."""
    row = conn.execute(
        "SELECT multiplier FROM unit_conversions WHERE unit_raw = ? AND unit_canonical = ?",
        (unit_raw, unit_canonical),
    ).fetchone()
    return row["multiplier"] if row else None


def normalize_concentration(conn: sqlite3.Connection, value: float, unit_raw: str,
                            target_unit: str, molecular_weight: float = None) -> tuple[float | None, str | None]:
    """Normalize a concentration to canonical units.
    
    Returns (normalized_value, canonical_unit) or (None, None) if no conversion found.
    """
    if unit_raw == target_unit:
        return value, target_unit
    
    multiplier = get_unit_conversion(conn, unit_raw, target_unit)
    if multiplier is not None:
        return value * multiplier, target_unit
    
    # MW-dependent: g/L → M requires molecular_weight
    if molecular_weight and unit_raw == "g/L" and target_unit == "M":
        return value / molecular_weight, "M"
    if molecular_weight and unit_raw == "mg/L" and target_unit == "M":
        return (value / 1000) / molecular_weight, "M"
    
    return None, None


# ── Query helpers ────────────────────────────────────────────

def get_paper(conn: sqlite3.Connection, paper_id: str) -> dict | None:
    """Get a paper by ID."""
    row = conn.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    return dict(row) if row else None


def get_paper_results(conn: sqlite3.Connection, paper_id: str) -> list[dict]:
    """Get all results for a paper."""
    rows = conn.execute(
        "SELECT * FROM results WHERE paper_id = ? ORDER BY experiment_id, sample_id",
        (paper_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_paper_experiments(conn: sqlite3.Connection, paper_id: str) -> list[dict]:
    """Get all experiments for a paper."""
    rows = conn.execute(
        "SELECT * FROM experiments WHERE paper_id = ? ORDER BY experiment_id",
        (paper_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_paper_stimuli(conn: sqlite3.Connection, paper_id: str) -> list[dict]:
    """Get all stimuli for a paper (joined with substance info)."""
    rows = conn.execute(
        """SELECT st.*, s.normalized_name AS substance_name, s.cas_number
           FROM stimuli st
           LEFT JOIN substances s ON st.substance_id = s.substance_id
           WHERE st.paper_id = ?
           ORDER BY st.stimulus_id""",
        (paper_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_paper_samples(conn: sqlite3.Connection, paper_id: str) -> list[dict]:
    """Get all samples for a paper with their components."""
    rows = conn.execute(
        "SELECT * FROM samples WHERE paper_id = ? ORDER BY sample_id",
        (paper_id,),
    ).fetchall()
    samples = [dict(r) for r in rows]
    for s in samples:
        comp_rows = conn.execute(
            """SELECT sc.*, st.original_name
               FROM sample_components sc
               LEFT JOIN stimuli st ON sc.stimulus_id = st.stimulus_id
               WHERE sc.sample_id = ?""",
            (s["sample_id"],),
        ).fetchall()
        s["components"] = [dict(c) for c in comp_rows]
    return samples


def delete_paper_data(conn: sqlite3.Connection, paper_id: str):
    """Delete all data for a paper (for re-extraction). Order matters for FK constraints."""
    conn.execute("DELETE FROM results WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM sample_components WHERE sample_id IN (SELECT sample_id FROM samples WHERE paper_id = ?)", (paper_id,))
    conn.execute("DELETE FROM samples WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM stimuli WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM experiments WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM extraction_runs WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
    conn.commit()
