#!/usr/bin/env python3
"""Base parser class and ParsedArticle dataclass for all publisher parsers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedTable:
    """A structured table extracted from an article."""
    table_id: str
    caption: str
    headers: list[str]
    rows: list[dict]  # Each row is a dict mapping header → value
    raw_html: str = ""

    def to_markdown(self) -> str:
        """Convert table to markdown format for LLM input."""
        if not self.headers:
            return ""
        lines = []
        if self.caption:
            lines.append(f"**{self.caption}**\n")
        lines.append("| " + " | ".join(self.headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(self.headers)) + " |")
        for row in self.rows:
            values = [str(row.get(h, "")) for h in self.headers]
            lines.append("| " + " | ".join(values) + " |")
        return "\n".join(lines)


@dataclass
class ParsedFigure:
    """A figure reference extracted from an article."""
    figure_id: str
    caption: str
    image_url: str
    local_path: str | None = None
    surrounding_text: str = ""


@dataclass
class ParsedArticle:
    """Complete parsed representation of a scientific article."""
    study_id: str
    doi: str
    publisher: str
    source_path: str  # Path to the raw HTML/XML file
    source_type: str  # "html", "xml", "pdf"

    # Article sections as plain text
    title: str = ""
    abstract: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    # Expected keys: "introduction", "methods", "results", "discussion", "references"

    # Structured data
    tables: list[ParsedTable] = field(default_factory=list)
    figures: list[ParsedFigure] = field(default_factory=list)

    # Full text (concatenated sections)
    full_text: str = ""

    # Parse quality indicators
    parse_confidence: float = 1.0  # 0.0 to 1.0
    parse_warnings: list[str] = field(default_factory=list)

    def get_section(self, name: str) -> str:
        """Get a section by name (case-insensitive, partial match)."""
        name_lower = name.lower()
        for key, value in self.sections.items():
            if name_lower in key.lower():
                return value
        return ""

    def get_methods_text(self) -> str:
        """Get combined methods/materials text."""
        parts = []
        for key in ["methods", "materials", "materials and methods",
                     "experimental", "experimental section"]:
            text = self.get_section(key)
            if text:
                parts.append(text)
        return "\n\n".join(parts) if parts else ""

    def get_results_text(self) -> str:
        """Get combined results text."""
        parts = []
        for key in ["results", "results and discussion", "findings"]:
            text = self.get_section(key)
            if text:
                parts.append(text)
        return "\n\n".join(parts) if parts else ""

    def get_tables_as_markdown(self) -> str:
        """Get all tables formatted as markdown for LLM input."""
        return "\n\n".join(t.to_markdown() for t in self.tables if t.headers)


class BaseParser(ABC):
    """Abstract base class for publisher-specific parsers."""

    publisher_name: str = "unknown"

    @abstractmethod
    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        """Parse an HTML/XML file into a ParsedArticle."""
        ...

    @abstractmethod
    def extract_sections(self, soup) -> dict[str, str]:
        """Extract named sections from the parsed document."""
        ...

    @abstractmethod
    def extract_tables(self, soup) -> list[ParsedTable]:
        """Extract structured tables from the parsed document."""
        ...

    @abstractmethod
    def extract_figures(self, soup, base_url: str = "") -> list[ParsedFigure]:
        """Extract figure references (URLs + captions) from the parsed document."""
        ...

    def _read_file(self, path: Path) -> str:
        """Read file content with encoding fallback."""
        for encoding in ["utf-8", "latin-1", "cp1252"]:
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise UnicodeDecodeError(f"Could not decode {path} with any supported encoding")

    def _clean_text(self, text: str) -> str:
        """Clean extracted text: normalize whitespace, strip artifacts."""
        import re
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'\[\d+\]', '', text)  # Remove reference markers like [1], [2]
        return text

    def _parse_html_table(self, table_element) -> tuple[list[str], list[dict]]:
        """Generic HTML table parser. Returns (headers, rows)."""
        headers = []
        rows = []

        # Extract headers from <thead> or first <tr>
        thead = table_element.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                headers = [
                    self._clean_text(th.get_text())
                    for th in header_row.find_all(["th", "td"])
                ]

        # Extract body rows
        tbody = table_element.find("tbody") or table_element
        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue

            # If we don't have headers yet, use first row as headers
            if not headers:
                headers = [self._clean_text(c.get_text()) for c in cells]
                continue

            row_data = {}
            for i, cell in enumerate(cells):
                key = headers[i] if i < len(headers) else f"col_{i}"
                row_data[key] = self._clean_text(cell.get_text())
            if row_data:
                rows.append(row_data)

        return headers, rows
