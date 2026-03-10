#!/usr/bin/env python3
"""Agent 1 — Free Extraction: Extract rich, flexible JSON from article text."""

import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.llm_extract import LLMClient, load_prompt

console = Console()


def run_agent1(article, study_id: str, config: dict = None,
               llm: LLMClient = None) -> dict:
    """Run Agent 1: Free extraction from parsed article.

    Args:
        article: ParsedArticle object from parse_article
        study_id: Paper identifier
        config: Config dict (loaded from config.yaml if None)
        llm: LLMClient instance (created if None)

    Returns:
        Rich, flexible JSON dict with all extracted information
    """
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    if llm is None:
        llm = LLMClient(config)

    console.print("  [dim]Agent 1: Free extraction...[/dim]")

    # Load prompt template
    prompt_template = load_prompt("agent1_free_extraction")

    # Build article content for prompt
    tables_md = _get_tables_markdown(article)
    article_text = _build_article_text(article)

    # Fill template
    prompt = prompt_template
    prompt = prompt.replace("{article_text}", article_text)
    prompt = prompt.replace("{tables_markdown}", tables_md)

    # Call LLM
    model = llm.get_model("agent1")
    result = llm.extract_json(prompt, model=model)

    # Ensure study_id is set
    if "study_metadata" in result:
        result["study_metadata"]["study_id"] = study_id

    console.print(f"  [green]✓ Agent 1 complete: {len(result.get('experiments', []))} experiments found[/green]")

    return result


def save_agent1_output(result: dict, study_id: str, config: dict = None) -> Path:
    """Save Agent 1 output as a versioned artifact."""
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    parts_dir = ROOT_DIR / config["paths"]["extractions_dir"] / "parts" / study_id
    parts_dir.mkdir(parents=True, exist_ok=True)

    output_path = parts_dir / "agent1_extraction.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return output_path


def _build_article_text(article) -> str:
    """Build the article text for the prompt, respecting context window limits."""
    parts = []
    if article.title:
        parts.append(f"TITLE: {article.title}")
    if article.abstract:
        parts.append(f"\nABSTRACT:\n{article.abstract}")

    for section_name, section_text in article.sections.items():
        parts.append(f"\n{section_name.upper()}:\n{section_text}")

    text = "\n".join(parts)
    # Limit to ~30K chars (~10K tokens) to leave room for prompt + output
    if len(text) > 30000:
        text = text[:30000] + "\n\n[... truncated for context window ...]"
    return text


def _get_tables_markdown(article) -> str:
    """Convert all tables to markdown format."""
    if not article.tables:
        return "(No tables found in article)"

    parts = []
    for table in article.tables:
        md = table.to_markdown() if hasattr(table, 'to_markdown') else str(table)
        parts.append(md)

    text = "\n\n".join(parts)
    if len(text) > 10000:
        text = text[:10000] + "\n\n[... additional tables truncated ...]"
    return text
