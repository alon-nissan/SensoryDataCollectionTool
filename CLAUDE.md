# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated pipeline for extracting sensory science data from published research papers into structured JSON, with a SQLite index for search/filtering. Uses Claude LLM (Sonnet for text, Opus for figure vision) via the Anthropic API.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in API keys

# ── Primary: process manually-downloaded files ──

# Single HTML or PDF file
python scripts/orchestrate.py --file data/html/smith2019.html
python scripts/orchestrate.py --file data/html/smith2019.html --doi "10.1093/chemse/28.3.219"
python scripts/orchestrate.py --file data/html/smith2019.pdf --study-id smith2019

# Batch: all HTML/PDF files in a directory
python scripts/orchestrate.py --input-dir data/html/

# Batch: CSV with file paths (columns: file_path, doi, study_id)
python scripts/orchestrate.py --file-list papers.csv

# ── Secondary: fetch by DOI (requires publisher access) ──

python scripts/orchestrate.py --doi "10.3390/nu10111632"
python scripts/orchestrate.py --doi-list papers.csv

# ── Common options ──

--skip-figures   # Skip figure download & vision extraction
--force          # Re-extract even if output exists
--validate       # Validate against gold standard
--dry-run        # Show what would be done

# Rebuild SQLite index from all extraction JSONs
python scripts/build_index.py

# Run individual pipeline steps
python scripts/parse_article.py <html_file>        # auto-detects publisher
python scripts/parse_article.py <html_file> oup     # explicit publisher
python scripts/extract_figures.py <doi>
python scripts/llm_extract.py <doi>
python scripts/assemble_json.py <doi>
python scripts/normalize_attributes.py <json_file>
python scripts/validate.py <json_file>

# Analysis notebooks
jupyter notebook analysis/
```

No test suite exists currently.

## Architecture

### Pipeline Flow (orchestrate.py)

**File-based (primary):**
`Local HTML/PDF → auto-detect publisher → parse → download figures → LLM extract (Prompts A-E) → assemble JSON → normalize → index in SQLite → flag gaps`

**DOI-based (secondary, requires publisher access):**
`DOI → resolve publisher → fetch HTML/XML → parse → download figures → LLM extract → assemble → normalize → index → flag gaps`

Publisher auto-detection (`scripts/parse_article.py: detect_publisher()`) checks `<meta>` tags, known domain markers, and CSS classes in the HTML. PDFs always route to `PDFParser`.

### Two-Layer Data Storage

- **Layer 1 — JSON files** (`data/extractions/`): Rich per-paper documents. Primary data store.
- **Layer 2 — SQLite** (`data/sensory_index.db`): Thin searchable catalog for filtering/discovery.

### Parser Hierarchy

`parsers/base_parser.py` defines `BaseParser` (ABC), `ParsedArticle`, `ParsedTable`, `ParsedFigure` dataclasses. Publisher-specific parsers inherit from `BaseParser`:
- `elsevier_parser.py`, `springer_parser.py`, `wiley_parser.py`, `mdpi_parser.py`, `oup_parser.py` — handle publisher-specific HTML/XML/JATS structure
- `generic_parser.py` — fallback for unknown publishers
- `pdf_parser.py` — PDF fallback when no HTML available

Publisher routing: auto-detected from file content, or configured in `config.yaml` under `publishers:`. `PARSER_MAP` in `scripts/parse_article.py` maps publisher key → parser class (includes `"pdf"` entry).

### LLM Extraction (scripts/llm_extract.py)

`LLMClient` wraps the Anthropic API with retry logic and cost tracking. Five specialized prompts in `prompts/`:
- **A**: metadata, **B**: experiment design, **C**: stimuli, **D**: sensory data (tables/text), **E**: figure data (vision, uses Opus)

Gold-standard JSONs in `data/gold_standard/` (wee2018, benabu2018) serve as few-shot examples in prompts.

### Key Configuration

- `.env` — API keys (Anthropic, Elsevier, Springer, Wiley) and institutional credentials
- `config.yaml` — model names, file paths, publisher mappings, extraction thresholds
- `vocabulary/attribute_map.json` — maps raw sensory attribute names to canonical forms

### Institutional Access (optional, DOI-based flow only)

`scripts/institutional_login.py` handles Shibboleth-based institutional login (HUJI) for publishers like OUP and Wiley. `scripts/export_cookies.py` reads Chrome cookies from disk via `browser-cookie3`.

**Note:** Automated fetching from paywalled publishers is unreliable. The recommended workflow is to download HTML/PDF files manually in the browser and use `--file` or `--input-dir` to process them.

Article fetching (`scripts/fetch_article.py`) uses a 6-layer fallback chain when DOI-based flow is used:
1. Direct HTTP (open access)
2. VPN-aware HTTP (auto-detects Samba VPN)
3. Saved authentication cookies
4. Unpaywall API (finds legal open-access versions)
5. Automated Shibboleth/OpenAthens login
6. PDF fallback

`scripts/fetch_validation.py` validates fetched HTML (detects paywall pages, Cloudflare challenges, incomplete content).

### scripts/ module resolution

Scripts use `ROOT_DIR = Path(__file__).resolve().parent.parent` and `sys.path.insert(0, str(ROOT_DIR))` to import from `parsers/`. Run scripts from the project root.
