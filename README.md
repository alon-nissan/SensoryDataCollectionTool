# Sensory Data Extraction Pipeline (v4)

Automated extraction of sensory science data from published research papers into a normalized SQLite database using a 4-agent LLM pipeline.

## Overview

This pipeline processes manually-downloaded HTML/PDF papers through four sequential LLM agents:

1. **Agent 1 — Free extraction** (Sonnet): Reads parsed article, produces rich flexible JSON
2. **Agent 2 — Structuring** (Sonnet): Maps JSON → relational SQLite rows across 10 tables
3. **Agent 3 — Figure extraction** (Opus, vision): Extracts data from figure images
4. **Agent 4 — Validation & correction** (Sonnet): Deterministic checks + targeted LLM corrections

## Quick Start

```bash
# 1. Set up environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure API key
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY

# 3. Initialize database
python scripts/init_db.py

# 4. Seed common substances (optional, improves substance resolution)
python scripts/substance_resolver.py

# 5. Process a single paper
python scripts/orchestrate.py --file data/html/smith2019.html

# 6. Process all papers in a directory
python scripts/orchestrate.py --input-dir data/html/
```

## CLI Usage

```bash
# Single file
python scripts/orchestrate.py --file data/html/smith2019.html

# Single file with DOI metadata tag
python scripts/orchestrate.py --file data/html/smith2019.html --doi "10.1093/chemse/28.3.219"

# All HTML/PDF files in a directory
python scripts/orchestrate.py --input-dir data/html/

# Batch from CSV (columns: file_path, doi, study_id)
python scripts/orchestrate.py --file-list papers.csv

# Options
--skip-figures     # Skip figure extraction (Agent 3)
--force            # Re-extract even if output exists
--validate-only    # Re-run validation (Agent 4) only
--dry-run          # Show what would be done
```

## Project Structure

```
├── data/
│   ├── html/              # Manually-downloaded HTML/PDF papers
│   ├── figures/            # Downloaded figure images
│   ├── extractions/        # Agent output JSONs
│   │   └── parts/          # Per-agent intermediate outputs (audit trail)
│   └── sensory_data.db     # Primary SQLite database (v4 schema)
├── prompts/                # Agent prompt templates (agent1–agent4)
├── parsers/                # HTML/XML and PDF article parsers
├── scripts/                # Pipeline scripts + orchestrator
├── vocabulary/             # Attribute normalization + substance seed data
├── analysis/               # Jupyter notebooks for research analysis
└── Plans and Ideas/        # Project planning documents
```

## Architecture

### 4-Agent Sequential Pipeline

```
Local HTML/PDF
  → detect file type → parse
  → Agent 1 (Sonnet, free extraction → rich JSON)
  → Agent 2 (Sonnet, structuring → SQLite rows)
  → Agent 3 (Opus, figure vision extraction)
  → Agent 4 (Sonnet, validation & correction)
  → SQLite database
```

### Two-Layer Data Storage

- **Layer 1 — SQLite database** (`data/sensory_data.db`): Primary data store with 10 relational tables.
- **Layer 2 — JSON artifacts** (`data/extractions/parts/`): Agent 1–4 outputs preserved for audit/debugging.

### Database Schema (v4)

| Table | Purpose |
|---|---|
| `papers` | One row per paper (metadata, DOI, food category, validation status) |
| `experiments` | One per experiment within a paper (method, scale, panel) |
| `substances` | Global chemical entity registry, cross-paper (normalized name, CAS, SMILES) |
| `substance_aliases` | Maps variant names → canonical `substance_id` |
| `stimuli` | Paper-specific sourced instances of substances (supplier, purity, form) |
| `samples` | What panelists actually tasted (label, base matrix, control flag) |
| `sample_components` | Junction: sample ↔ stimulus with concentration + canonical units |
| `results` | Core data: sample × attribute → value (with error, source, confidence) |
| `extraction_runs` | Audit trail: prompt versions, models, cost, validation report |
| `unit_conversions` | Deterministic unit conversion rules |

### Parsers

`parsers/base_parser.py` defines `BaseParser` (ABC) with four abstract methods and dataclasses: `ParsedArticle`, `ParsedTable`, `ParsedFigure`. Two parsers inherit from it:
- **Generic** (`generic_parser.py`) — enhanced HTML/XML parser that consolidates extraction patterns from all major publishers (Elsevier, Springer, Wiley, MDPI, OUP) into a multi-strategy cascade
- **PDF** (`pdf_parser.py`) — fallback for PDF files

File type detection in `scripts/parse_article.py` routes `.pdf` files to `PDFParser` and everything else to `GenericParser`.

## Configuration

- **`.env`** — `ANTHROPIC_API_KEY` (only key needed)
- **`config.yaml`** — Per-agent model names, prompt versions, file paths, extraction settings
- **`vocabulary/attribute_map.json`** — Maps raw sensory attribute names to canonical forms
- **`vocabulary/substances_seed.json`** — Seed data for the substances table

## Requirements

- Python 3.10+
- Anthropic API key (Claude Sonnet + Opus access)
