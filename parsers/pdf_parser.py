#!/usr/bin/env python3
"""PDF fallback parser using Marker or basic text extraction."""

from pathlib import Path

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class PDFParser(BaseParser):
    """Fallback parser for PDF files. Uses marker-pdf if available, else basic extraction."""

    publisher_name = "pdf"

    def parse(self, pdf_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        try:
            return self._parse_with_marker(pdf_path, doi, study_id)
        except ImportError:
            print("  ⚠ marker-pdf not installed. Attempting basic PDF text extraction...")
            return self._parse_basic(pdf_path, doi, study_id)

    def _parse_with_marker(self, pdf_path: Path, doi: str, study_id: str) -> ParsedArticle:
        """Parse PDF using marker-pdf library."""
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict

        model_dict = create_model_dict()
        converter = PdfConverter(artifact_dict=model_dict)
        result = converter(str(pdf_path))

        # marker returns markdown — split into sections
        markdown_text = result.markdown
        sections = self._split_markdown_sections(markdown_text)

        return ParsedArticle(
            study_id=study_id or pdf_path.stem,
            doi=doi,
            publisher=self.publisher_name,
            source_path=str(pdf_path),
            source_type="pdf",
            sections=sections,
            full_text=markdown_text,
            parse_confidence=0.6,  # PDF parsing is less reliable
            parse_warnings=["Parsed from PDF — verify table and figure extraction accuracy"],
        )

    def _parse_basic(self, pdf_path: Path, doi: str, study_id: str) -> ParsedArticle:
        """Minimal PDF text extraction without marker."""
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
        except ImportError:
            text = f"[PDF file at {pdf_path} — install marker-pdf or PyMuPDF for text extraction]"

        return ParsedArticle(
            study_id=study_id or pdf_path.stem,
            doi=doi,
            publisher=self.publisher_name,
            source_path=str(pdf_path),
            source_type="pdf",
            full_text=text,
            parse_confidence=0.3,
            parse_warnings=[
                "Basic PDF extraction only — no structured sections, tables, or figures",
                "Install marker-pdf for better PDF parsing: pip install marker-pdf",
            ],
        )

    def _split_markdown_sections(self, markdown: str) -> dict[str, str]:
        """Split marker's markdown output into named sections."""
        sections = {}
        current_section = "preamble"
        current_text = []

        for line in markdown.split("\n"):
            if line.startswith("## "):
                if current_text:
                    sections[current_section] = "\n".join(current_text).strip()
                current_section = line[3:].strip().lower()
                current_text = []
            else:
                current_text.append(line)

        if current_text:
            sections[current_section] = "\n".join(current_text).strip()

        return sections

    def extract_sections(self, soup) -> dict[str, str]:
        return {}

    def extract_tables(self, soup) -> list[ParsedTable]:
        return []

    def extract_figures(self, soup, base_url: str = "") -> list[ParsedFigure]:
        return []
