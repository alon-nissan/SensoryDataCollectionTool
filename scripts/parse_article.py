#!/usr/bin/env python3
"""Parse a fetched HTML/XML article using the appropriate publisher parser."""

import sys
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from parsers.mdpi_parser import MDPIParser
from parsers.elsevier_parser import ElsevierParser
from parsers.springer_parser import SpringerParser
from parsers.wiley_parser import WileyParser
from parsers.generic_parser import GenericParser
from parsers.oup_parser import OUPParser
from parsers.base_parser import ParsedArticle


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


PARSER_MAP = {
    "mdpi": MDPIParser,
    "elsevier": ElsevierParser,
    "springer": SpringerParser,
    "wiley": WileyParser,
    "oup": OUPParser,
    "generic": GenericParser,
}


def get_parser(publisher: str):
    """Get the appropriate parser for a publisher."""
    parser_class = PARSER_MAP.get(publisher, GenericParser)
    return parser_class()


def parse_article(html_path: Path, publisher: str, doi: str = "", study_id: str = "") -> ParsedArticle:
    """Parse an article HTML/XML file using the appropriate publisher parser."""
    parser = get_parser(publisher)
    article = parser.parse(html_path, doi=doi, study_id=study_id)

    print(f"  Parser: {parser.publisher_name}")
    print(f"  Title: {article.title[:80]}...")
    print(f"  Sections: {list(article.sections.keys())}")
    print(f"  Tables: {len(article.tables)}")
    print(f"  Figures: {len(article.figures)}")
    print(f"  Confidence: {article.parse_confidence:.1%}")
    if article.parse_warnings:
        for w in article.parse_warnings:
            print(f"  ⚠ {w}")

    return article


def main():
    if len(sys.argv) < 3:
        print("Usage: python parse_article.py <html_path> <publisher> [doi] [study_id]")
        print("Example: python parse_article.py data/html/wee2018.html mdpi 10.3390/nu10111632 wee2018")
        sys.exit(1)

    html_path = Path(sys.argv[1])
    publisher = sys.argv[2]
    doi = sys.argv[3] if len(sys.argv) > 3 else ""
    study_id = sys.argv[4] if len(sys.argv) > 4 else ""

    article = parse_article(html_path, publisher, doi, study_id)
    print(f"\n✅ Parsed successfully: {article.study_id}")


if __name__ == "__main__":
    main()
