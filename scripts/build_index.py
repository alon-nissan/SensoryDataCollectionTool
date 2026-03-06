#!/usr/bin/env python3
"""Build and manage the SQLite sensory data index from extracted JSON files."""

import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def get_db_path(config=None):
    if config is None:
        config = load_config()
    return ROOT_DIR / config["paths"]["sqlite_db"]


def create_schema(conn: sqlite3.Connection):
    """Create the sensory index table with all 20 fields."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sensory_index (
            study_id TEXT PRIMARY KEY,
            doi TEXT,
            title TEXT,
            year INTEGER,
            journal TEXT,
            country TEXT,
            food_category TEXT,
            num_experiments INTEGER,
            sensory_methods TEXT,
            scale_types TEXT,
            attributes_measured TEXT,
            total_stimuli INTEGER,
            total_panelists INTEGER,
            panel_types TEXT,
            has_dose_response BOOLEAN,
            has_mixture_stimuli BOOLEAN,
            has_figure_data BOOLEAN,
            num_data_gaps INTEGER,
            extraction_date TEXT,
            validation_status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()


def extract_index_fields(json_path: Path) -> dict:
    """Extract the ~20 index fields from a paper's JSON file."""
    with open(json_path) as f:
        data = json.load(f)

    meta = data.get("study_metadata", {})
    experiments = data.get("experiments", [])
    extraction_meta = data.get("extraction_metadata", {})
    figure_inventory = data.get("figure_inventory", [])

    # Collect sensory methods, scale types, attributes, panel types across experiments
    sensory_methods = set()
    scale_types = set()
    attributes = set()
    panel_types = set()
    total_stimuli = 0
    max_panelists = 0
    has_dose_response = False
    has_mixture = False

    for exp in experiments:
        # Scale info
        scale = exp.get("scale", {})
        if scale.get("type"):
            scale_types.add(scale["type"])
            sensory_methods.add(scale.get("full_name", scale["type"]))

        # Panel info
        panel = exp.get("panel", {})
        if panel.get("panel_type"):
            panel_types.add(panel["panel_type"])
        panel_size = panel.get("panel_size", 0)
        if panel_size and panel_size > max_panelists:
            max_panelists = panel_size

        # Stimuli
        stimuli = exp.get("stimuli", [])
        total_stimuli += len(stimuli)
        for stim in stimuli:
            comp = stim.get("composition", {})
            if len(comp) > 1:
                has_mixture = True

        # Sensory data — check for dose-response
        sensory_data = exp.get("sensory_data", {})
        if "dose_response" in str(sensory_data).lower():
            has_dose_response = True

        # Derived metrics
        derived = exp.get("derived_metrics", {})
        if derived:
            has_dose_response = True

        # Collect attributes from sensory_data keys
        if isinstance(sensory_data, dict):
            for key in sensory_data:
                if key not in ("notes", "source", "data_source"):
                    attributes.add(key)

    # Figure data
    has_figure_data = any(
        fig.get("extraction_status") == "extracted" or fig.get("data_extracted")
        for fig in figure_inventory
    )

    # Data gaps
    num_data_gaps = len(extraction_meta.get("data_gaps", []))

    return {
        "study_id": meta.get("study_id", json_path.stem),
        "doi": meta.get("doi"),
        "title": meta.get("title"),
        "year": meta.get("year"),
        "journal": meta.get("journal"),
        "country": meta.get("country"),
        "food_category": meta.get("food_category"),
        "num_experiments": len(experiments),
        "sensory_methods": ", ".join(sorted(sensory_methods)) or None,
        "scale_types": ", ".join(sorted(scale_types)) or None,
        "attributes_measured": ", ".join(sorted(attributes)) or None,
        "total_stimuli": total_stimuli,
        "total_panelists": max_panelists,
        "panel_types": ", ".join(sorted(panel_types)) or None,
        "has_dose_response": has_dose_response,
        "has_mixture_stimuli": has_mixture,
        "has_figure_data": has_figure_data,
        "num_data_gaps": num_data_gaps,
        "extraction_date": extraction_meta.get("extraction_date"),
        "validation_status": extraction_meta.get("validation_status", "pending"),
    }


def upsert_row(conn: sqlite3.Connection, fields: dict):
    """Insert or replace a row in the sensory index."""
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    conn.execute(
        f"INSERT OR REPLACE INTO sensory_index ({columns}) VALUES ({placeholders})",
        list(fields.values()),
    )
    conn.commit()


def build_index(extractions_dir: Path, db_path: Path):
    """Build the full index from all JSON files in the extractions directory."""
    conn = sqlite3.connect(db_path)
    create_schema(conn)

    json_files = list(extractions_dir.glob("*.json"))
    if not json_files:
        print("No JSON files found in extractions directory.")
        conn.close()
        return

    for json_path in sorted(json_files):
        try:
            fields = extract_index_fields(json_path)
            upsert_row(conn, fields)
            print(f"  ✓ Indexed: {fields['study_id']}")
        except Exception as e:
            print(f"  ✗ Error indexing {json_path.name}: {e}")

    conn.close()
    print(f"\nIndex built with {len(json_files)} papers → {db_path}")


def query_index(db_path: Path, where_clause: str = None) -> list[dict]:
    """Query the index with an optional WHERE clause."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM sensory_index"
    if where_clause:
        sql += f" WHERE {where_clause}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def main():
    config = load_config()
    db_path = get_db_path(config)
    extractions_dir = ROOT_DIR / config["paths"]["extractions_dir"]

    if len(sys.argv) > 1 and sys.argv[1] == "--create-only":
        conn = sqlite3.connect(db_path)
        create_schema(conn)
        conn.close()
        print(f"Schema created: {db_path}")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--paper":
        paper_id = sys.argv[2]
        json_path = extractions_dir / f"{paper_id}.json"
        if not json_path.exists():
            print(f"File not found: {json_path}")
            sys.exit(1)
        conn = sqlite3.connect(db_path)
        create_schema(conn)
        fields = extract_index_fields(json_path)
        upsert_row(conn, fields)
        conn.close()
        print(f"Indexed: {paper_id}")
        return

    # Full rebuild
    print(f"Building index from {extractions_dir}...")
    build_index(extractions_dir, db_path)


if __name__ == "__main__":
    main()
