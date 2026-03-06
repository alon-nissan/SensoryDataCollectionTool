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

# Run full pipeline for a single paper
python scripts/orchestrate.py --doi "10.3390/nu10111632"

# With options
python scripts/orchestrate.py --doi "10.xxxx/yyyy" --study-id wee2018
python scripts/orchestrate.py --doi "10.xxxx/yyyy" --skip-figures
python scripts/orchestrate.py --doi "10.xxxx/yyyy" --dry-run
python scripts/orchestrate.py --doi "10.xxxx/yyyy" --validate

# Batch extraction
python scripts/orchestrate.py --doi-list papers.csv

# Rebuild SQLite index from all extraction JSONs
python scripts/build_index.py

# Run individual pipeline steps
python scripts/fetch_article.py <doi>
python scripts/parse_article.py <html_file>
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

`DOI → resolve publisher → fetch HTML/XML → parse → download figures → LLM extract (Prompts A-E) → assemble JSON → normalize attributes → index in SQLite → flag gaps`

### Two-Layer Data Storage

- **Layer 1 — JSON files** (`data/extractions/`): Rich per-paper documents. Primary data store.
- **Layer 2 — SQLite** (`data/sensory_index.db`): Thin searchable catalog for filtering/discovery.

### Parser Hierarchy

`parsers/base_parser.py` defines `BaseParser` (ABC), `ParsedArticle`, `ParsedTable`, `ParsedFigure` dataclasses. Publisher-specific parsers inherit from `BaseParser`:
- `elsevier_parser.py`, `springer_parser.py`, `wiley_parser.py`, `mdpi_parser.py`, `oup_parser.py` — handle publisher-specific HTML/XML/JATS structure
- `generic_parser.py` — fallback for unknown publishers
- `pdf_parser.py` — PDF fallback when no HTML available

Publisher routing is configured in `config.yaml` under `publishers:` (domain → parser mapping).

### LLM Extraction (scripts/llm_extract.py)

`LLMClient` wraps the Anthropic API with retry logic and cost tracking. Five specialized prompts in `prompts/`:
- **A**: metadata, **B**: experiment design, **C**: stimuli, **D**: sensory data (tables/text), **E**: figure data (vision, uses Opus)

Gold-standard JSONs in `data/gold_standard/` (wee2018, benabu2018) serve as few-shot examples in prompts.

### Key Configuration

- `.env` — API keys (Anthropic, Elsevier, Springer, Wiley) and institutional credentials
- `config.yaml` — model names, file paths, publisher mappings, extraction thresholds
- `vocabulary/attribute_map.json` — maps raw sensory attribute names to canonical forms

### Institutional Access

`scripts/institutional_login.py` handles Shibboleth-based institutional login (HUJI) via Selenium for publishers like OUP and Wiley that require it. Uses the user's real Chrome browser via remote debugging to bypass Cloudflare.

Features: automated Shibboleth login (institution selection + credential entry), cookie persistence (`data/_cookies/`), session reuse across batch runs. Only pauses for manual CAPTCHA solving.

Article fetching uses a 6-layer fallback chain (in `scripts/fetch_article.py`):
1. Direct HTTP (open access)
2. VPN-aware HTTP (auto-detects Samba VPN)
3. Saved authentication cookies
4. Unpaywall API (finds legal open-access versions)
5. Automated Shibboleth/OpenAthens login
6. PDF fallback

`scripts/fetch_validation.py` validates fetched HTML (detects paywall pages, Cloudflare challenges, incomplete content).

### scripts/ module resolution

Scripts use `ROOT_DIR = Path(__file__).resolve().parent.parent` and `sys.path.insert(0, str(ROOT_DIR))` to import from `parsers/`. Run scripts from the project root.
