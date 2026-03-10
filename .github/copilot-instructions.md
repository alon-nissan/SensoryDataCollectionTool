# Copilot Instructions

## Project Overview

Automated pipeline for extracting sensory science data from published research papers into a normalized SQLite database. Papers are manually downloaded as HTML/PDF and processed through a 4-agent LLM pipeline (Claude via Anthropic API) that extracts, structures, validates, and stores sensory data.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # set ANTHROPIC_API_KEY (only key needed)
python scripts/init_db.py  # create SQLite schema + seed unit conversions

# Process papers
python scripts/orchestrate.py --file data/html/smith2019.html
python scripts/orchestrate.py --input-dir data/html/
python scripts/orchestrate.py --file-list papers.csv

# Options: --skip-figures, --force, --validate-only, --dry-run

# Run individual pipeline steps
python scripts/parse_article.py <file_path>          # auto-detects HTML vs PDF
python scripts/extract_figures.py <paper_id>
python scripts/normalize_attributes.py <json_file>
python scripts/validate.py <json_file>
```

No test suite exists.

## Architecture

### 4-Agent Pipeline (`orchestrate.py`)

```
Local HTML/PDF → detect file type → parse
  → Agent 1 (Sonnet): free extraction → rich JSON
  → Agent 2 (Sonnet): structuring → SQLite rows
  → Agent 3 (Opus, vision): figure data extraction
  → Agent 4 (Sonnet): validation & correction → SQLite
```

Each agent has a dedicated script (`scripts/agent{1-4}_*.py`) and prompt template (`prompts/agent{1-4}_*.txt`). Prompts are plain text with Python string formatting markers, loaded by `LLMClient` in `scripts/llm_extract.py`.

### Two-Layer Data Storage

- **Primary — SQLite** (`data/sensory_data.db`): 10 relational tables (`papers`, `experiments`, `substances`, `substance_aliases`, `stimuli`, `samples`, `sample_components`, `results`, `extraction_runs`, `unit_conversions`).
- **Audit trail — JSON** (`data/extractions/parts/<paper_id>/`): Per-agent intermediate outputs preserved for debugging.

### Parser Hierarchy

`parsers/base_parser.py` defines `BaseParser` (ABC) with four abstract methods: `parse()`, `extract_sections()`, `extract_tables()`, `extract_figures()`. Also defines dataclasses: `ParsedArticle`, `ParsedTable`, `ParsedFigure`.

Two parsers: `generic_parser.py` (enhanced HTML/XML parser consolidating patterns from all major publishers) and `pdf_parser.py` (PDF fallback).

File type detection in `scripts/parse_article.py: detect_file_type()` routes `.pdf` files to `PDFParser`, everything else to `GenericParser`. `PARSER_MAP` maps file type keys → parser classes.

## Key Conventions

### Module Resolution

All scripts resolve the project root and add it to `sys.path` for cross-package imports:

```python
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
```

Always run scripts from the project root.

### Logging

Uses `rich.console.Console` for all output — no standard `logging` module:

```python
from rich.console import Console
console = Console()
console.print("[bold green]✓[/] Done")
console.print("[red]✗ Error:[/]", str(e))
```

### Anthropic API

All LLM calls go through `LLMClient` in `scripts/llm_extract.py`. Two methods:
- `extract_json()` — text-only (Agents 1, 2, 4)
- `extract_json_with_image()` — vision calls with base64 images (Agent 3)

Includes retry with exponential backoff for rate limits and per-model cost tracking.

### Environment

Single env var: `ANTHROPIC_API_KEY` in `.env`, loaded via `python-dotenv`. Model names and all other config live in `config.yaml`.

### Vocabulary

`vocabulary/attribute_map.json` maps raw sensory terms → canonical forms (e.g., `"sweet" → "sweetness"`). `vocabulary/substances_seed.json` seeds the global substances registry.
