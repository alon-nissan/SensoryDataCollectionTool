#!/usr/bin/env python3
"""Migration: v5 → v6 schema.

v6 changes:
- New 'panels' table: panel as a first-class measuring device entity
- New columns on observations: panel_id (FK → panels), measurement_domain
- Panel info stays accessible; existing observations get measurement_domain='sensory' by default

This migration is NON-DESTRUCTIVE:
- The panels table is created fresh (it didn't exist in v5)
- The two new observation columns are added via ALTER TABLE
- Existing rows get panel_id=NULL and measurement_domain='sensory' (safe defaults)
- Panel data from context_json is NOT automatically migrated (would require LLM re-run)
  because the v5 context_json panel format was unstructured free text.

Usage:
    python scripts/migrate_v5_to_v6.py
    python scripts/migrate_v5_to_v6.py --db /path/to/custom.db
    python scripts/migrate_v5_to_v6.py --dry-run   # show what would be done without doing it
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import yaml
from rich.console import Console

console = Console()


def _load_db_path() -> Path:
    config_path = ROOT_DIR / "config.yaml"
    default = ROOT_DIR / "data" / "sensory_data.db"
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        rel = cfg.get("paths", {}).get("sqlite_db")
        if rel:
            return ROOT_DIR / rel
    return default


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def migrate(db_path: Path, dry_run: bool = False) -> None:
    if not db_path.exists():
        console.print(f"[red]Database not found at {db_path}[/red]")
        sys.exit(1)

    console.print(f"[bold]Migrating database:[/bold] {db_path}")
    if dry_run:
        console.print("[yellow]DRY RUN — no changes will be written[/yellow]")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=OFF")  # disable during migration
    conn.row_factory = sqlite3.Row

    steps_done = 0
    steps_skipped = 0

    # ── Step 1: Create panels table ──────────────────────────────────────────
    if _table_exists(conn, "panels"):
        console.print("  [dim]Step 1: panels table already exists — skipped[/dim]")
        steps_skipped += 1
    else:
        sql = """
        CREATE TABLE panels (
            panel_id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(paper_id),
            parent_panel_id TEXT REFERENCES panels(panel_id),
            panel_label TEXT NOT NULL,
            panel_size INTEGER,
            attributes_json TEXT,
            description TEXT
        )
        """
        if not dry_run:
            conn.execute(sql)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_panels_paper ON panels(paper_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_panels_parent ON panels(parent_panel_id)")
            conn.commit()
        console.print("  [green]✓ Step 1: Created panels table[/green]")
        steps_done += 1

    # ── Step 2: Add panel_id column to observations ──────────────────────────
    if _column_exists(conn, "observations", "panel_id"):
        console.print("  [dim]Step 2: panel_id column already exists — skipped[/dim]")
        steps_skipped += 1
    else:
        if not dry_run:
            conn.execute("ALTER TABLE observations ADD COLUMN panel_id TEXT REFERENCES panels(panel_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_panel ON observations(panel_id)")
            conn.commit()
        console.print("  [green]✓ Step 2: Added panel_id column to observations (NULL for all existing rows)[/green]")
        steps_done += 1

    # ── Step 3: Add measurement_domain column to observations ─────────────────
    if _column_exists(conn, "observations", "measurement_domain"):
        console.print("  [dim]Step 3: measurement_domain column already exists — skipped[/dim]")
        steps_skipped += 1
    else:
        if not dry_run:
            conn.execute(
                "ALTER TABLE observations ADD COLUMN measurement_domain TEXT NOT NULL DEFAULT 'sensory'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_domain ON observations(measurement_domain)"
            )
            conn.commit()
        console.print("  [green]✓ Step 3: Added measurement_domain column to observations (default 'sensory')[/green]")
        steps_done += 1

    # ── Step 4: Report what panel data exists in context_json ────────────────
    console.print("\n  [bold]Step 4: Auditing panel data in existing context_json...[/bold]")
    papers = conn.execute("SELECT paper_id, context_json FROM papers").fetchall()
    papers_with_panel_data = 0
    for paper in papers:
        paper_id = paper["paper_id"]
        ctx_raw = paper["context_json"]
        if not ctx_raw:
            continue
        try:
            ctx = json.loads(ctx_raw)
            exps = ctx.get("experiments", {})
            for exp_key, exp_data in exps.items():
                panel = exp_data.get("panel")
                if panel and isinstance(panel, dict) and any(panel.values()):
                    papers_with_panel_data += 1
                    console.print(
                        f"    [yellow]Paper '{paper_id}' / {exp_key} has panel data in context_json "
                        f"— will need re-extraction or manual migration[/yellow]"
                    )
        except (json.JSONDecodeError, AttributeError):
            pass

    if papers_with_panel_data == 0:
        console.print("    [dim]No panel data found in context_json[/dim]")
    else:
        console.print(
            f"\n  [bold yellow]NOTE:[/bold yellow] {papers_with_panel_data} paper(s) have panel data "
            "in context_json that has NOT been migrated to the new panels table.\n"
            "  To fully populate the panels table, re-run the extraction pipeline on these papers."
        )

    # Re-enable foreign keys
    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()

    # Summary
    console.print(f"\n[bold green]Migration complete:[/bold green] "
                  f"{steps_done} steps applied, {steps_skipped} already done.")
    if dry_run:
        console.print("[yellow]DRY RUN — no changes were written to disk[/yellow]")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Migrate sensory-data SQLite schema from v5 to v6.")
    parser.add_argument("--db", type=str, default=None, help="Override database path")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else _load_db_path()
    migrate(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
