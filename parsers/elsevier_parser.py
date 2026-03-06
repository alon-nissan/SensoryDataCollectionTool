#!/usr/bin/env python3
"""Elsevier/ScienceDirect publisher parser."""

from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class ElsevierParser(BaseParser):
    """Parser for Elsevier articles (ScienceDirect XML/HTML)."""

    publisher_name = "elsevier"

    # Common Elsevier XML namespaces
    NAMESPACES = {
        "ce": "http://www.elsevier.com/xml/common/dtd",
        "ja": "http://www.elsevier.com/xml/ja/dtd",
        "xlink": "http://www.w3.org/1999/xlink",
    }

    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        content = self._read_file(html_path)

        # Detect if XML or HTML
        is_xml = content.strip().startswith("<?xml") or "<ce:" in content
        parser = "lxml-xml" if is_xml else "lxml"
        soup = BeautifulSoup(content, parser)

        if is_xml:
            return self._parse_xml(soup, html_path, doi, study_id)
        return self._parse_html(soup, html_path, doi, study_id)

    def _parse_html(self, soup, html_path, doi, study_id) -> ParsedArticle:
        """Parse Elsevier HTML (ScienceDirect web pages)."""
        # Title
        title_el = soup.find("span", class_="title-text") or soup.find("h1")
        title = self._clean_text(title_el.get_text()) if title_el else ""

        # Abstract
        abstract_el = soup.find("div", class_="abstract") or soup.find("div", {"id": "abstracts"})
        abstract = self._clean_text(abstract_el.get_text()) if abstract_el else ""

        sections = self.extract_sections(soup)
        tables = self.extract_tables(soup)
        figures = self.extract_figures(soup, base_url="https://www.sciencedirect.com")

        full_text = "\n\n".join(
            [f"# {title}", abstract] + [f"## {k}\n{v}" for k, v in sections.items()]
        )

        article = ParsedArticle(
            study_id=study_id or html_path.stem,
            doi=doi,
            publisher=self.publisher_name,
            source_path=str(html_path),
            source_type="html",
            title=title,
            abstract=abstract,
            sections=sections,
            tables=tables,
            figures=figures,
            full_text=full_text,
        )

        if not sections:
            article.parse_warnings.append("No sections found in Elsevier HTML")
            article.parse_confidence *= 0.5

        return article

    def _parse_xml(self, soup, html_path, doi, study_id) -> ParsedArticle:
        """Parse Elsevier XML (API response)."""
        # Title from XML
        title_el = soup.find("ce:title") or soup.find("dc:title") or soup.find("title")
        title = self._clean_text(title_el.get_text()) if title_el else ""

        # Abstract
        abstract_el = soup.find("ce:abstract") or soup.find("dc:description")
        abstract = self._clean_text(abstract_el.get_text()) if abstract_el else ""

        sections = self._extract_xml_sections(soup)
        tables = self._extract_xml_tables(soup)
        figures = self._extract_xml_figures(soup)

        full_text = "\n\n".join(
            [f"# {title}", abstract] + [f"## {k}\n{v}" for k, v in sections.items()]
        )

        article = ParsedArticle(
            study_id=study_id or html_path.stem,
            doi=doi,
            publisher=self.publisher_name,
            source_path=str(html_path),
            source_type="xml",
            title=title,
            abstract=abstract,
            sections=sections,
            tables=tables,
            figures=figures,
            full_text=full_text,
        )

        if not sections:
            article.parse_warnings.append("No sections found in Elsevier XML")
            article.parse_confidence *= 0.5

        return article

    def extract_sections(self, soup) -> dict[str, str]:
        """Extract sections from Elsevier HTML."""
        sections = {}

        # ScienceDirect uses <section> with class="section-paragraph"
        for section_el in soup.find_all("div", class_="section-paragraph"):
            heading = section_el.find_previous(["h2", "h3"])
            if not heading:
                continue
            name = self._clean_text(heading.get_text()).lower()
            text = self._clean_text(section_el.get_text())
            if text:
                if name in sections:
                    sections[name] += " " + text
                else:
                    sections[name] = text

        # Fallback: generic heading-based extraction
        if not sections:
            for h2 in soup.find_all("h2"):
                name = self._clean_text(h2.get_text()).lower()
                parts = []
                for sib in h2.find_next_siblings():
                    if sib.name == "h2":
                        break
                    parts.append(self._clean_text(sib.get_text()))
                if parts:
                    sections[name] = " ".join(parts)

        return sections

    def _extract_xml_sections(self, soup) -> dict[str, str]:
        """Extract sections from Elsevier XML."""
        sections = {}
        for sec in soup.find_all("ce:section"):
            title_el = sec.find("ce:section-title")
            if not title_el:
                continue
            name = self._clean_text(title_el.get_text()).lower()
            # Get all paragraph text
            paras = sec.find_all("ce:para")
            text = " ".join(self._clean_text(p.get_text()) for p in paras)
            if text:
                sections[name] = text
        return sections

    def extract_tables(self, soup) -> list[ParsedTable]:
        tables = []
        # ScienceDirect HTML tables
        for i, table_el in enumerate(soup.find_all("table"), 1):
            caption_el = table_el.find_previous("div", class_="table-caption")
            if not caption_el:
                caption_el = table_el.find("caption")
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Table {i}"

            headers, rows = self._parse_html_table(table_el)
            if headers:
                tables.append(ParsedTable(
                    table_id=f"table_{i}",
                    caption=caption,
                    headers=headers,
                    rows=rows,
                    raw_html=str(table_el),
                ))
        return tables

    def _extract_xml_tables(self, soup) -> list[ParsedTable]:
        """Extract tables from Elsevier XML."""
        tables = []
        for i, table_el in enumerate(soup.find_all("ce:table"), 1):
            caption_el = table_el.find("ce:caption")
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Table {i}"

            # Try to find <table> inside <ce:table>
            inner_table = table_el.find("table")
            if inner_table:
                headers, rows = self._parse_html_table(inner_table)
                if headers:
                    tables.append(ParsedTable(
                        table_id=f"table_{i}",
                        caption=caption,
                        headers=headers,
                        rows=rows,
                        raw_html=str(table_el),
                    ))
        return tables

    def extract_figures(self, soup, base_url: str = "") -> list[ParsedFigure]:
        figures = []
        for i, fig_el in enumerate(soup.find_all(["figure", "div"], class_=lambda c: c and "figure" in str(c).lower()), 1):
            img = fig_el.find("img")
            if not img:
                continue

            img_url = img.get("src", "") or img.get("data-src", "")
            if img_url and not img_url.startswith("http"):
                img_url = urljoin(base_url, img_url)

            caption_el = fig_el.find(["figcaption", "div"], class_=lambda c: c and "caption" in str(c).lower())
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Figure {i}"

            prev_p = fig_el.find_previous("p")
            surrounding = self._clean_text(prev_p.get_text()) if prev_p else ""

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=img_url,
                surrounding_text=surrounding[:500],
            ))
        return figures

    def _extract_xml_figures(self, soup) -> list[ParsedFigure]:
        """Extract figure references from Elsevier XML."""
        figures = []
        for i, fig_el in enumerate(soup.find_all("ce:figure"), 1):
            caption_el = fig_el.find("ce:caption")
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Figure {i}"

            # Image reference in XML
            graphic = fig_el.find("ce:e-component") or fig_el.find("graphic")
            img_url = ""
            if graphic:
                img_url = graphic.get("xlink:href", "") or graphic.get("id", "")

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=img_url,
            ))
        return figures
