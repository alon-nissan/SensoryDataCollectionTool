# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated pipeline for extracting sensory science data from published research papers into a normalized SQLite database. Uses a 4-agent LLM architecture (Claude Sonnet for text, Opus for figure vision) via the Anthropic API. Papers are manually downloaded as HTML/PDF and processed locally.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in ANTHROPIC_API_KEY

# Initialize database (first time)
python scripts/init_db.py

# Seed common substances
python scripts/substance_resolver.py

# Process files
python scripts/orchestrate.py --file data/html/smith2019.html
python scripts/orchestrate.py --file data/html/smith2019.html --doi "10.1093/chemse/28.3.219"
python scripts/orchestrate.py --input-dir data/html/
python scripts/orchestrate.py --file-list papers.csv

# Options
--skip-figures     # Skip figure extraction (Agent 3)
--force            # Re-extract even if output exists
--validate-only    # Re-run validation (Agent 4) only
--from-agent3      # Resume from Agent 3 (load cached Agent 1 & 2 artifacts)
--dry-run          # Show what would be done

# Run individual pipeline steps
python scripts/parse_article.py <file_path>          # auto-detects HTML vs PDF
python scripts/parse_article.py <file_path> <doi> <study_id>
python scripts/extract_figures.py <paper_id>
python scripts/normalize_attributes.py <json_file>
python scripts/validate.py <json_file>

# Analysis notebooks
jupyter notebook analysis/
```

No test suite exists currently.

## Architecture

### Pipeline Flow (orchestrate.py)

```
Local HTML/PDF → detect file type (HTML/PDF) → parse → Agent 1 (free extraction → rich JSON)
  → Agent 2 (structuring → SQLite rows) → Agent 3 (figure vision extraction)
  → Agent 4 (validation & correction) → SQLite database
```

`File → parse → Agent 1 (Sonnet, free extraction) → Agent 2 (Sonnet, structuring) → Agent 3 (Opus, figures) → Agent 4 (Sonnet, validation) → SQLite`

File type detection (`scripts/parse_article.py: detect_file_type()`) routes `.pdf` files to `PDFParser` and all other files to the enhanced `GenericParser`.

### Two-Layer Data Storage

- **Layer 1 — SQLite database** (`data/sensory_data.db`): Primary data store. 7 relational tables (v5 schema).
- **Layer 2 — JSON artifacts** (`data/extractions/parts/`): Agent 1–4 outputs + peripheral context JSON preserved for audit/debugging.

### Database Schema (v5)

Flat, denormalized schema. The old 5-level FK chain (substance → stimulus → sample_component → sample → result) was replaced with a single `observations` table. Peripheral metadata (panel info, sourcing, design) lives in JSON documents per paper.

| Table | Purpose |
|---|---|
| `papers` | One row per paper (paper_id, DOI, title, year, journal, validation_status) |
| `experiments` | One per experiment within a paper (method, scale_type, scale_range) |
| `observations` | Core data: substance × attribute → value. Denormalized — each row is self-contained with substance_name, components_json (full composition array with concentrations), base_matrix, attribute, value, error, source. Supports sample-level, mixture (multi-element components_json), and stimulus-level derived metrics (components_json=NULL, value_type='derived_param'). |
| `substances` | Global chemical entity registry, cross-paper (normalized name, CAS, SMILES) |
| `substance_aliases` | Maps variant names → canonical `substance_id` |
| `extraction_runs` | Audit trail: prompt versions, models, cost, validation report |
| `unit_conversions` | Deterministic unit conversion rules (seeded by `init_db.py`) |

Key design: LLM agents produce flat observation rows + peripheral JSON. Deterministic Python code handles ID generation (`{paper_id}__exp{N}`), substance registry resolution, and DB commits. No IDs cross-referenced by the LLM.

### Parser Hierarchy

`parsers/base_parser.py` defines `BaseParser` (ABC), `ParsedArticle`, `ParsedTable`, `ParsedFigure` dataclasses. `ParsedTable` includes an `extraction_method` field (`"deterministic"` | `"vision"`) indicating how the table was extracted. Two parsers inherit from `BaseParser`:

- `generic_parser.py` — enhanced HTML/XML parser consolidating extraction patterns from all major publishers (Elsevier, Springer, Wiley, MDPI, OUP). `_parse_html_table()` supports colspan/rowspan via a grid-based cell expansion approach.
- `pdf_parser.py` — **hybrid table extraction**: primary extraction uses `pdfplumber` for deterministic table detection with a confidence heuristic (header quality, column consistency, cell fill rate). Low-confidence tables fall back to Claude vision (renders the table region as an image → Opus vision call via `extract_json_with_image()`). Controlled by the `table_extraction` section in `config.yaml`.

File type routing: `detect_file_type()` in `scripts/parse_article.py` routes `.pdf` files to `PDFParser`, all other files to `GenericParser`. `PARSER_MAP` maps file type key → parser class. `parse_article.py` now accepts optional `config` and `llm` parameters to support the vision fallback path. `orchestrate.py` creates the LLM client before parsing so vision costs are tracked.

### 4-Agent LLM Extraction

`LLMClient` in `scripts/llm_extract.py` wraps the Anthropic API with retry logic and cost tracking. Four specialized agents with prompts in `prompts/`:

- **Agent 1 — Free extraction** (`agent1_extract.py`, Sonnet): Reads parsed article and produces a rich, flexible JSON capturing all sensory data without schema constraints.
- **Agent 2 — Structuring** (`agent2_structure.py`, Sonnet): Transforms Agent 1's JSON into flat observation rows + peripheral context JSON. No ID generation — uses simple experiment labels (exp1, exp2) and substance names as text.
- **Agent 3 — Figure extraction** (`agent3_figures.py`, Opus, vision): Extracts data from figure images using Claude's vision capability. Outputs flat observations matching Agent 2's format, with dedup against existing data.
- **Agent 4 — Validation & correction** (`agent4_validate.py`, Sonnet): Two-level validation — L1 deterministic checks (missing fields, unit consistency, range plausibility) + L2 targeted LLM corrections for flagged issues.

### Key Scripts

| Script | Role |
|---|---|
| `orchestrate.py` | Top-level CLI; runs the full pipeline |
| `agent1_extract.py` | Agent 1: free extraction |
| `agent2_structure.py` | Agent 2: structuring into DB rows |
| `agent3_figures.py` | Agent 3: figure vision extraction |
| `agent4_validate.py` | Agent 4: validation & correction |
| `init_db.py` | Create/upgrade SQLite schema + seed unit conversions |
| `db.py` | Database access layer (connections, queries, inserts) |
| `paper_id.py` | Deterministic paper ID generation |
| `substance_resolver.py` | Substance alias resolution (deterministic + LLM fallback) |
| `parse_article.py` | File type detection (HTML/PDF) + parse dispatch |
| `extract_figures.py` | Figure image download |
| `llm_extract.py` | `LLMClient` wrapper for Anthropic API |
| `normalize_attributes.py` | Sensory attribute normalization |
| `validate.py` | Standalone validation utilities |
| `migrate_v4_to_v5.py` | One-time migration from v4 (10-table) to v5 (7-table) schema |

### Key Configuration

- `.env` — `ANTHROPIC_API_KEY` (only key needed)
- `config.yaml` — per-agent model names, prompt versions, file paths, extraction settings (confidence threshold, spot-check fraction, etc.), and `table_extraction` section (vision fallback model, confidence thresholds). **IMPORTANT: When modifying any prompt file in `prompts/`, always bump the corresponding version in `config.yaml → prompt_versions`. The pipeline records these in `extraction_runs` for reproducibility.**
- `vocabulary/attribute_map.json` — maps raw sensory attribute names to canonical forms
- `vocabulary/substances_seed.json` — seed data for the `substances` table

### scripts/ module resolution

Scripts use `ROOT_DIR = Path(__file__).resolve().parent.parent` and `sys.path.insert(0, str(ROOT_DIR))` to import from `parsers/`. Run scripts from the project root.
