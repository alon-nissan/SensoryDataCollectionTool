#!/usr/bin/env python3
"""Initialize the v4 SQLite database schema for sensory data extraction.

Usage:
    python scripts/init_db.py          # creates/updates data/sensory_data.db
    python scripts/init_db.py --db /path/to/custom.db

Importable:
    from scripts.init_db import init_database
"""

import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import yaml
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

TABLES_SQL = """
-- 1. papers: One row per paper
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    doi TEXT UNIQUE,
    title TEXT,
    year INTEGER,
    journal TEXT,
    country TEXT,
    food_category TEXT,
    num_experiments INTEGER,
    panel_types TEXT,
    max_panel_size INTEGER,
    has_figure_data BOOLEAN DEFAULT 0,
    has_supplementary_data BOOLEAN DEFAULT 0,
    data_availability TEXT,
    data_availability_details TEXT,
    latest_run_id INTEGER REFERENCES extraction_runs(run_id),
    validation_status TEXT DEFAULT 'pending',
    context_json TEXT
);

-- 2. experiments: One per experiment within a paper
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    experiment_label TEXT,
    sensory_method TEXT,
    scale_type TEXT,
    scale_range TEXT,
    panel_size INTEGER,
    panel_type TEXT,
    serving_temp_c REAL,
    serving_temp_raw TEXT,
    conditions_json TEXT
);

-- 3. substances: Global chemical entity registry (cross-paper)
CREATE TABLE IF NOT EXISTS substances (
    substance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name TEXT UNIQUE NOT NULL,
    cas_number TEXT,
    smiles TEXT,
    molecular_weight REAL,
    category TEXT,
    properties_json TEXT
);

-- 4. substance_aliases: Maps variant names to canonical substances
CREATE TABLE IF NOT EXISTS substance_aliases (
    alias TEXT PRIMARY KEY,
    substance_id INTEGER NOT NULL REFERENCES substances(substance_id)
);

-- 5. stimuli: Paper-specific sourced instances of substances
CREATE TABLE IF NOT EXISTS stimuli (
    stimulus_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    substance_id INTEGER NOT NULL REFERENCES substances(substance_id),
    original_name TEXT,
    supplier TEXT,
    purity TEXT,
    form TEXT,
    details_json TEXT
);

-- 6. samples: What panelists actually tasted
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    sample_label TEXT,
    base_matrix TEXT,
    is_control BOOLEAN DEFAULT 0
);

-- 7. sample_components: Junction table (sample ↔ stimulus with concentration)
CREATE TABLE IF NOT EXISTS sample_components (
    sample_id TEXT NOT NULL REFERENCES samples(sample_id),
    stimulus_id TEXT NOT NULL REFERENCES stimuli(stimulus_id),
    concentration REAL,
    unit TEXT,
    concentration_canonical REAL,
    unit_canonical TEXT,
    PRIMARY KEY (sample_id, stimulus_id)
);

-- 8. results: Core data — sample × attribute → value
CREATE TABLE IF NOT EXISTS results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    sample_id TEXT REFERENCES samples(sample_id),
    attribute_raw TEXT,
    attribute_normalized TEXT,
    attribute_category TEXT,
    value REAL,
    value_type TEXT,
    error_value REAL,
    error_type TEXT,
    n INTEGER,
    source_type TEXT,
    source_location TEXT,
    extraction_confidence TEXT,
    run_id INTEGER REFERENCES extraction_runs(run_id),
    context_json TEXT
);

-- 9. extraction_runs: Audit trail for pipeline runs
CREATE TABLE IF NOT EXISTS extraction_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    run_timestamp TEXT NOT NULL,
    agent1_prompt_version TEXT,
    agent2_prompt_version TEXT,
    agent3_prompt_version TEXT,
    agent4_prompt_version TEXT,
    model_versions TEXT,
    status TEXT DEFAULT 'in_progress',
    validation_report TEXT,
    corrections_applied INTEGER DEFAULT 0,
    human_review_items INTEGER DEFAULT 0,
    token_usage TEXT,
    total_cost_usd REAL DEFAULT 0.0,
    notes TEXT
);

-- 10. unit_conversions: Deterministic conversion rules
CREATE TABLE IF NOT EXISTS unit_conversions (
    unit_raw TEXT NOT NULL,
    unit_canonical TEXT NOT NULL,
    multiplier REAL NOT NULL,
    category TEXT,
    PRIMARY KEY (unit_raw, unit_canonical)
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_experiments_paper ON experiments(paper_id);
CREATE INDEX IF NOT EXISTS idx_stimuli_paper ON stimuli(paper_id);
CREATE INDEX IF NOT EXISTS idx_stimuli_substance ON stimuli(substance_id);
CREATE INDEX IF NOT EXISTS idx_samples_paper ON samples(paper_id);
CREATE INDEX IF NOT EXISTS idx_samples_experiment ON samples(experiment_id);
CREATE INDEX IF NOT EXISTS idx_results_paper ON results(paper_id);
CREATE INDEX IF NOT EXISTS idx_results_experiment ON results(experiment_id);
CREATE INDEX IF NOT EXISTS idx_results_sample ON results(sample_id);
CREATE INDEX IF NOT EXISTS idx_results_attribute ON results(attribute_normalized);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_paper ON extraction_runs(paper_id);
CREATE INDEX IF NOT EXISTS idx_substance_aliases_substance ON substance_aliases(substance_id);
"""

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

UNIT_CONVERSIONS = [
    ("mM", "M", 0.001, "solutions"),
    ("µM", "M", 0.000001, "solutions"),
    ("% w/v", "g/L", 10, "solutions"),
    ("% w/w", "g/kg", 10, "formulated_food"),
    ("ppm", "mg/L", 1, "solutions"),
    ("ppm", "mg/kg", 1, "formulated_food"),
    ("ppb", "µg/L", 1, "solutions"),
    ("g/100mL", "g/L", 10, "solutions"),
    ("mg/mL", "g/L", 1, "solutions"),
    ("µg/mL", "mg/L", 1, "solutions"),
]


def seed_unit_conversions(conn: sqlite3.Connection) -> int:
    """Insert common unit conversion rules. Returns the number of rows inserted."""
    inserted = 0
    for unit_raw, unit_canonical, multiplier, category in UNIT_CONVERSIONS:
        try:
            conn.execute(
                "INSERT INTO unit_conversions (unit_raw, unit_canonical, multiplier, category) "
                "VALUES (?, ?, ?, ?)",
                (unit_raw, unit_canonical, multiplier, category),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # already exists
    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _load_db_path() -> Path:
    """Resolve the database path from config.yaml, falling back to default."""
    config_path = ROOT_DIR / "config.yaml"
    default = ROOT_DIR / "data" / "sensory_data.db"
    if config_path.exists():
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        rel = cfg.get("paths", {}).get("sqlite_db")
        if rel:
            return ROOT_DIR / rel
    return default


def init_database(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create / upgrade the v4 schema and return an open connection.

    Parameters
    ----------
    db_path : Path or str, optional
        Override for the database file location.  When *None* the path is
        read from ``config.yaml`` → ``paths.sqlite_db``, falling back to
        ``data/sensory_data.db``.
    """
    if db_path is None:
        db_path = _load_db_path()
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))

    # Pragmas
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    # Tables
    conn.executescript(TABLES_SQL)

    # Indexes
    conn.executescript(INDEXES_SQL)

    # Seed data
    inserted = seed_unit_conversions(conn)

    return conn


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Initialize v4 sensory-data SQLite schema.")
    parser.add_argument("--db", type=str, default=None, help="Override database path")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else _load_db_path()

    console.print(f"[bold]Initializing database:[/bold] {db_path}")

    conn = init_database(db_path)

    # Report
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    console.print(f"[green]✓[/green] Created {len(tables)} tables: {', '.join(tables)}")

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    indexes = [row[0] for row in cursor.fetchall()]
    console.print(f"[green]✓[/green] Created {len(indexes)} indexes")

    cursor = conn.execute("SELECT COUNT(*) FROM unit_conversions")
    conv_count = cursor.fetchone()[0]
    console.print(f"[green]✓[/green] Seeded {conv_count} unit conversions")

    conn.close()
    console.print("[bold green]Database ready.[/bold green]")


if __name__ == "__main__":
    main()
