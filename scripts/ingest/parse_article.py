#!/usr/bin/env python3
"""Parse a local HTML/XML/PDF article using the appropriate parser."""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from parsers.generic_parser import GenericParser
from parsers.pdf_parser import PDFParser
from parsers.base_parser import ParsedArticle


PARSER_MAP = {
    "html": GenericParser,
    "pdf": PDFParser,
}


def detect_file_type(file_path: Path) -> str:
    """Determine whether a file should be parsed as PDF or HTML.

    Returns "pdf" for .pdf files, "html" for everything else
    (HTML, XML, XHTML are all handled by GenericParser).
    """
    if file_path.suffix.lower() == ".pdf":
        return "pdf"
    return "html"


def get_parser(file_type: str, config: dict = None, llm=None):
    """Get the appropriate parser for a file type."""
    parser_class = PARSER_MAP.get(file_type, GenericParser)
    if file_type == "pdf":
        return parser_class(config=config, llm=llm)
    return parser_class()


def parse_article(file_path: Path, file_type: str = None, doi: str = "",
                  study_id: str = "", config: dict = None, llm=None) -> ParsedArticle:
    """Parse an article file using the appropriate parser.

    Args:
        file_path: Path to HTML/XML/PDF file
        file_type: "html" or "pdf" (auto-detected if None)
        doi: DOI string if known
        study_id: Study identifier if known
        config: Pipeline config dict (enables PDF vision fallback)
        llm: LLMClient instance (enables PDF vision fallback)

    Returns:
        ParsedArticle with sections, tables, figures, and metadata
    """
    if file_type is None:
        file_type = detect_file_type(file_path)

    parser = get_parser(file_type, config=config, llm=llm)
    article = parser.parse(file_path, doi=doi, study_id=study_id)

    print(f"  Parser: {parser.publisher_name}")
    print(f"  Title: {article.title[:80]}..." if article.title else "  Title: (not found)")
    print(f"  Sections: {list(article.sections.keys())}")
    print(f"  Tables: {len(article.tables)}")
    print(f"  Figures: {len(article.figures)}")
    print(f"  Confidence: {article.parse_confidence:.1%}")
    if article.parse_warnings:
        for w in article.parse_warnings:
            print(f"  ⚠ {w}")

    return article


def main():
    if len(sys.argv) < 2:
        print("Usage: python parse_article.py <file_path> [doi] [study_id]")
        print("  File type (HTML/PDF) is auto-detected from extension.")
        print("Example: python parse_article.py data/html/wee2018.html")
        print("Example: python parse_article.py data/pdf/smith2019.pdf 10.1093/chemse/28.3.219 smith2019")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    doi = sys.argv[2] if len(sys.argv) > 2 else ""
    study_id = sys.argv[3] if len(sys.argv) > 3 else ""

    file_type = detect_file_type(file_path)
    print(f"  File type: {file_type}")

    article = parse_article(file_path, file_type, doi, study_id)
    print(f"\n✅ Parsed successfully: {article.study_id}")


if __name__ == "__main__":
    main()
