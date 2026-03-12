#!/usr/bin/env python3
"""PDF parser with hybrid table extraction: pdfplumber (deterministic) + vision fallback."""

import logging
import tempfile
from pathlib import Path

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable

logger = logging.getLogger(__name__)


class PDFParser(BaseParser):
    """PDF parser using marker-pdf for text, pdfplumber for tables, vision fallback for complex tables."""

    publisher_name = "pdf"

    # Minimum confidence from pdfplumber before falling back to vision
    TABLE_CONFIDENCE_THRESHOLD = 0.4

    def __init__(self, config: dict | None = None, llm=None):
        self.config = config or {}
        self.llm = llm  # Optional LLMClient for vision fallback

        # Allow config override for threshold
        table_config = self.config.get("table_extraction", {})
        self.vision_fallback_threshold = table_config.get(
            "vision_fallback_threshold", self.TABLE_CONFIDENCE_THRESHOLD
        )
        self.enable_vision_fallback = table_config.get("enable_vision_fallback", True)

    def parse(self, pdf_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        try:
            article = self._parse_with_marker(pdf_path, doi, study_id)
        except ImportError:
            logger.warning("marker-pdf not installed. Attempting basic PDF text extraction...")
            article = self._parse_basic(pdf_path, doi, study_id)

        # Extract tables with pdfplumber (independent of marker-pdf text extraction)
        tables = self._extract_tables_from_pdf(pdf_path)
        if tables:
            article.tables = tables
            article.parse_confidence = max(article.parse_confidence, 0.7)
            # Remove stale warning about tables if we found some
            article.parse_warnings = [
                w for w in article.parse_warnings
                if "table" not in w.lower() and "verify table" not in w.lower()
            ]

        return article

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
            parse_confidence=0.6,
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

    # ── Table extraction ─────────────────────────────────────────────

    def _extract_tables_from_pdf(self, pdf_path: Path) -> list[ParsedTable]:
        """Extract tables using pdfplumber, with optional vision fallback."""
        try:
            import pdfplumber
        except ImportError:
            logger.warning("pdfplumber not installed — skipping PDF table extraction")
            return []

        tables: list[ParsedTable] = []
        table_idx = 0

        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    page_tables = page.extract_tables()
                    if not page_tables:
                        continue

                    for raw_table in page_tables:
                        if not raw_table or len(raw_table) < 2:
                            continue

                        table_idx += 1
                        parsed = self._raw_table_to_parsed(raw_table, table_idx, page_num)
                        if not parsed:
                            continue

                        confidence = self._assess_table_confidence(parsed)

                        if (confidence < self.vision_fallback_threshold
                                and self.enable_vision_fallback and self.llm):
                            # Try vision fallback
                            vision_table = self._extract_table_vision(
                                pdf_path, page, raw_table, table_idx, page_num
                            )
                            if vision_table:
                                tables.append(vision_table)
                                continue

                        tables.append(parsed)

        except Exception as e:
            logger.warning(f"pdfplumber table extraction failed: {e}")

        return tables

    def _raw_table_to_parsed(
        self, raw_table: list[list], table_idx: int, page_num: int
    ) -> ParsedTable | None:
        """Convert pdfplumber's raw table (list of lists) to a ParsedTable."""
        # Clean cells: replace None with empty string, strip whitespace
        cleaned = []
        for row in raw_table:
            cleaned.append([
                (cell.strip() if isinstance(cell, str) else "")
                for cell in (row or [])
            ])

        if not cleaned:
            return None

        # First row as headers
        headers = cleaned[0]

        # Skip if all headers are empty
        if all(h == "" for h in headers):
            if len(cleaned) > 1:
                headers = cleaned[1]
                cleaned = cleaned[1:]
            else:
                return None

        # Build row dicts
        rows = []
        for row in cleaned[1:]:
            row_dict = {}
            for i, value in enumerate(row):
                key = headers[i] if i < len(headers) else f"col_{i}"
                row_dict[key] = value
            if any(v.strip() for v in row_dict.values()):
                rows.append(row_dict)

        if not headers or not rows:
            return None

        return ParsedTable(
            table_id=f"table_{table_idx}",
            caption=f"Table {table_idx} (page {page_num})",
            headers=headers,
            rows=rows,
            extraction_method="deterministic",
        )

    def _assess_table_confidence(self, table: ParsedTable) -> float:
        """Heuristic confidence score for a pdfplumber-extracted table.

        Checks:
        - Header quality: non-empty, unique headers
        - Column consistency: rows have same number of values as headers
        - Cell fill rate: fraction of non-empty cells
        """
        score = 1.0

        # Header quality
        non_empty_headers = [h for h in table.headers if h.strip()]
        if not non_empty_headers:
            return 0.0
        header_quality = len(non_empty_headers) / len(table.headers)
        unique_headers = len(set(non_empty_headers)) / len(non_empty_headers)
        score *= (header_quality * 0.5 + unique_headers * 0.5)

        # Column consistency
        if table.rows:
            expected_cols = len(table.headers)
            consistent_rows = sum(
                1 for row in table.rows if len(row) == expected_cols
            )
            score *= (consistent_rows / len(table.rows))

        # Cell fill rate
        if table.rows:
            total_cells = sum(len(row) for row in table.rows)
            filled_cells = sum(
                1 for row in table.rows for v in row.values() if v.strip()
            )
            fill_rate = filled_cells / total_cells if total_cells > 0 else 0
            score *= (0.5 + 0.5 * fill_rate)  # Partial penalty for empty cells

        return round(score, 3)

    def _extract_table_vision(
        self, pdf_path: Path, page, raw_table, table_idx: int, page_num: int
    ) -> ParsedTable | None:
        """Render a table region as image and extract via Claude vision."""
        try:
            # Get table bounding box from the raw table data
            found_tables = page.find_tables()
            if not found_tables:
                return None

            # Use the first matching table's bbox (heuristic: pick table closest to our index)
            bbox_idx = min(table_idx - 1, len(found_tables) - 1)
            table_obj = found_tables[bbox_idx]
            bbox = table_obj.bbox

            # Crop page to table region and render as image
            cropped = page.crop(bbox)
            img = cropped.to_image(resolution=200)

            # Save to temporary file
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                img.save(tmp.name)
                tmp_path = tmp.name

            # Send to vision model
            prompt = (
                "Extract the table from this image as structured JSON.\n"
                "Return ONLY a JSON object with:\n"
                '  "headers": ["list", "of", "column", "headers"],\n'
                '  "rows": [{"header1": "value1", "header2": "value2"}, ...]\n'
                "Preserve all numeric values exactly as shown. "
                "Use empty string for empty cells."
            )

            result = self.llm.extract_json_with_image(
                prompt, tmp_path, model=self.llm.vision_model
            )

            # Clean up temp file
            Path(tmp_path).unlink(missing_ok=True)

            headers = result.get("headers", [])
            raw_rows = result.get("rows", [])
            if not headers or not raw_rows:
                return None

            # Convert to row dicts if rows are lists
            rows = []
            for row in raw_rows:
                if isinstance(row, dict):
                    rows.append(row)
                elif isinstance(row, list):
                    row_dict = {}
                    for i, val in enumerate(row):
                        key = headers[i] if i < len(headers) else f"col_{i}"
                        row_dict[key] = str(val)
                    rows.append(row_dict)

            return ParsedTable(
                table_id=f"table_{table_idx}",
                caption=f"Table {table_idx} (page {page_num})",
                headers=headers,
                rows=rows,
                extraction_method="vision",
            )

        except Exception as e:
            logger.warning(f"Vision table extraction failed for table {table_idx}: {e}")
            return None

    # ── Markdown section splitting ───────────────────────────────────

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
