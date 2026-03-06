#!/usr/bin/env python3
"""Parse a fetched HTML/XML article using the appropriate publisher parser."""

import re
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
from parsers.pdf_parser import PDFParser
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
    "pdf": PDFParser,
    "generic": GenericParser,
}

# Domain / HTML markers → publisher key (checked in order, first match wins)
_PUBLISHER_MARKERS = [
    ("oup", [
        r"academic\.oup\.com",
        r'class="[^"]*wi-article-title',
        r'class="[^"]*at-articleTitle',
        r"Oxford University Press",
    ]),
    ("mdpi", [
        r"mdpi\.com",
        r'class="[^"]*html-body',
        r"Published by MDPI",
    ]),
    ("elsevier", [
        r"elsevier\.com",
        r"sciencedirect\.com",
        r'class="[^"]*Elsevier',
        r"Published by Elsevier",
    ]),
    ("springer", [
        r"springer\.com",
        r"springernature\.com",
        r"nature\.com",
        r"BioMed Central",
        r"SpringerLink",
    ]),
    ("wiley", [
        r"wiley\.com",
        r"onlinelibrary\.wiley\.com",
        r'class="[^"]*article-section',
        r"John Wiley",
    ]),
]


def detect_publisher(file_path: Path) -> str:
    """Auto-detect publisher from a local file.

    For PDF files → always returns "pdf".
    For HTML/XML → scans content for known publisher markers.
    Falls back to "generic" if no match found.
    """
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return "pdf"

    # Read enough of the file for detection (first 50KB is plenty)
    try:
        text = file_path.read_text(errors="replace")[:50_000]
    except Exception:
        return "generic"

    # XML JATS detection
    if suffix == ".xml":
        if "dtd-version" in text and ("<article" in text or "<front>" in text):
            if "elsevier" in text.lower():
                return "elsevier"
            if "springer" in text.lower():
                return "springer"
            return "generic"

    # HTML: check citation_publisher meta tag first
    pub_meta = re.search(
        r'<meta\s+name="citation_publisher"\s+content="([^"]+)"', text, re.I
    )
    if pub_meta:
        pub_name = pub_meta.group(1).lower()
        if "oxford" in pub_name or "oup" in pub_name:
            return "oup"
        if "mdpi" in pub_name:
            return "mdpi"
        if "elsevier" in pub_name:
            return "elsevier"
        if "springer" in pub_name or "nature" in pub_name or "biomed" in pub_name:
            return "springer"
        if "wiley" in pub_name:
            return "wiley"

    # HTML: scan for domain / class markers
    for publisher_key, patterns in _PUBLISHER_MARKERS:
        for pattern in patterns:
            if re.search(pattern, text, re.I):
                return publisher_key

    return "generic"


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
    if len(sys.argv) < 2:
        print("Usage: python parse_article.py <html_path> [publisher] [doi] [study_id]")
        print("  publisher is auto-detected if omitted")
        print("Example: python parse_article.py data/html/wee2018.html")
        print("Example: python parse_article.py data/html/wee2018.html mdpi 10.3390/nu10111632 wee2018")
        sys.exit(1)

    html_path = Path(sys.argv[1])
    publisher = sys.argv[2] if len(sys.argv) > 2 else None
    doi = sys.argv[3] if len(sys.argv) > 3 else ""
    study_id = sys.argv[4] if len(sys.argv) > 4 else ""

    if publisher is None:
        publisher = detect_publisher(html_path)
        print(f"  Auto-detected publisher: {publisher}")

    article = parse_article(html_path, publisher, doi, study_id)
    print(f"\n✅ Parsed successfully: {article.study_id}")


if __name__ == "__main__":
    main()
