#!/usr/bin/env python3
"""MDPI publisher parser (Nutrients, Foods, Molecules, etc.)."""

from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class MDPIParser(BaseParser):
    """Parser for MDPI open-access journal articles (HTML)."""

    publisher_name = "mdpi"

    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        content = self._read_file(html_path)
        soup = BeautifulSoup(content, "lxml")

        # Extract title
        title_el = soup.find("h1", class_="title") or soup.find("h1")
        title = self._clean_text(title_el.get_text()) if title_el else ""

        # Extract abstract
        abstract_el = soup.find("div", class_="art-abstract")
        if not abstract_el:
            abstract_el = soup.find("section", {"id": "abstract"})
        abstract = self._clean_text(abstract_el.get_text()) if abstract_el else ""

        sections = self.extract_sections(soup)
        tables = self.extract_tables(soup)
        figures = self.extract_figures(soup, base_url=f"https://www.mdpi.com")

        # Build full text
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

        # Quality checks
        if not sections:
            article.parse_warnings.append("No sections found")
            article.parse_confidence *= 0.5
        if not tables:
            article.parse_warnings.append("No tables found")

        return article

    def extract_sections(self, soup) -> dict[str, str]:
        sections = {}

        # MDPI uses <section> elements with specific IDs or heading-based structure
        for section_el in soup.find_all("section"):
            heading = section_el.find(["h2", "h3", "h4"])
            if not heading:
                continue
            section_name = self._clean_text(heading.get_text()).lower()

            # Remove the heading text from section content
            heading_text = heading.get_text()
            section_text = section_el.get_text()
            section_text = section_text.replace(heading_text, "", 1).strip()
            section_text = self._clean_text(section_text)

            if section_text:
                sections[section_name] = section_text

        # If <section> tags didn't work, try heading-based extraction
        if not sections:
            sections = self._extract_by_headings(soup)

        return sections

    def _extract_by_headings(self, soup) -> dict[str, str]:
        """Fallback: extract sections by finding <h2> tags and collecting text until next <h2>."""
        sections = {}
        headings = soup.find_all("h2")
        for i, h2 in enumerate(headings):
            name = self._clean_text(h2.get_text()).lower()
            parts = []
            for sibling in h2.find_next_siblings():
                if sibling.name == "h2":
                    break
                text = self._clean_text(sibling.get_text())
                if text:
                    parts.append(text)
            if parts:
                sections[name] = " ".join(parts)
        return sections

    def extract_tables(self, soup) -> list[ParsedTable]:
        tables = []
        for i, table_el in enumerate(soup.find_all("table"), 1):
            # Look for caption
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

    def extract_figures(self, soup, base_url: str = "") -> list[ParsedFigure]:
        figures = []
        for i, fig_el in enumerate(soup.find_all("figure"), 1):
            img = fig_el.find("img")
            if not img:
                continue

            # Get image URL
            img_url = img.get("src", "") or img.get("data-src", "")
            if img_url and not img_url.startswith("http"):
                img_url = urljoin(base_url, img_url)

            # Get caption
            caption_el = fig_el.find("figcaption")
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Figure {i}"

            # Get surrounding text (previous paragraph)
            prev_p = fig_el.find_previous("p")
            surrounding = self._clean_text(prev_p.get_text()) if prev_p else ""

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=img_url,
                surrounding_text=surrounding[:500],
            ))

        return figures
