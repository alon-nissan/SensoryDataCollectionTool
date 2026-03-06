# Sensory Data Extraction Pipeline

Automated extraction of sensory science data from published research papers into structured JSON format, with a searchable SQLite index.

## Overview

This pipeline:
1. **Fetches** scientific articles via DOI (HTML/XML from publisher APIs, PDF fallback)
2. **Parses** them into structured sections, tables, and figure images
3. **Extracts** sensory data using Claude LLM (Sonnet for text, Opus for figure vision)
4. **Assembles** validated JSON files (one per paper) following a consistent schema
5. **Indexes** papers in a SQLite database for filtering and discovery

## Quick Start

```bash
# 1. Set up environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure API keys
cp .env.example .env
# Edit .env with your Anthropic API key and publisher API keys

# 3. Extract a single paper
python scripts/orchestrate.py --doi "10.3390/nu10111632"

# 4. Extract a batch of papers
python scripts/orchestrate.py --doi-list papers.csv

# 5. Rebuild the SQLite index
python scripts/build_index.py
```

## Project Structure

```
├── data/
│   ├── html/              # Raw HTML/XML from publishers
│   ├── figures/            # Downloaded figure images
│   ├── extractions/        # LLM-generated JSON files (one per paper)
│   ├── gold_standard/      # Manually validated JSONs (Wee 2018, Ben Abu 2018)
│   └── sensory_index.db    # SQLite searchable index
├── prompts/                # LLM prompt templates (A through E)
├── parsers/                # Publisher-specific HTML/XML parsers
├── scripts/                # Pipeline scripts + orchestrator
├── vocabulary/             # Attribute normalization mappings
├── analysis/               # Jupyter notebooks for research analysis
└── Plans and Ideas/        # Project planning documents
```

## Architecture

### Two-Layer Data Storage

- **Layer 1 — JSON files**: Rich, flexible documents capturing everything from each paper. Primary data store.
- **Layer 2 — SQLite index**: Thin searchable catalog (~20 fields per paper) for filtering and discovery.

### LLM Extraction Strategy

Five specialized prompts, each producing a section of the output JSON:
- **Prompt A**: Study metadata (title, authors, journal, etc.)
- **Prompt B**: Experiment design (panel, session, scale)
- **Prompt C**: Stimuli (compounds, concentrations, compositions)
- **Prompt D**: Sensory data + derived metrics (from tables and text)
- **Prompt E**: Figure data extraction (vision-based, using Claude Opus)

Gold-standard JSONs (Wee 2018, Ben Abu 2018) are used as few-shot examples in all prompts.

## Configuration

- **`.env`**: API keys (Anthropic, Elsevier, Springer, Wiley)
- **`config.yaml`**: Model settings, file paths, publisher mappings, extraction parameters

## Requirements

- Python 3.10+
- Anthropic API key (Claude Sonnet + Opus access)
- Publisher API keys (optional, for non-open-access papers)
- Institutional VPN (for some publisher access)
