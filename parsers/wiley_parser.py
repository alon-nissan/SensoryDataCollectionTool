#!/usr/bin/env python3
"""Wiley Online Library publisher parser."""

from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class WileyParser(BaseParser):
    """Parser for Wiley Online Library articles (HTML/XML)."""

    publisher_name = "wiley"

    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        content = self._read_file(html_path)
        soup = BeautifulSoup(content, "lxml")

        title_el = (
            soup.find("h1", class_="citation__title")
            or soup.find("span", class_="article-header__title")
            or soup.find("h1")
        )
        title = self._clean_text(title_el.get_text()) if title_el else ""

        abstract_el = (
            soup.find("div", class_="article-section__abstract")
            or soup.find("section", class_="article-section--abstract")
            or soup.find("div", {"id": "abstract"})
        )
        abstract = self._clean_text(abstract_el.get_text()) if abstract_el else ""

        sections = self.extract_sections(soup)
        tables = self.extract_tables(soup)
        figures = self.extract_figures(soup, base_url="https://onlinelibrary.wiley.com")

        full_text = "\n\n".join(
            [f"# {title}", abstract] + [f"## {k}\n{v}" for k, v in sections.items()]
        )

        return ParsedArticle(
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

    def extract_sections(self, soup) -> dict[str, str]:
        sections = {}

        # Wiley uses <section class="article-section"> with <h2> headings
        for sec in soup.find_all("section", class_="article-section__content"):
            heading = sec.find_previous(["h2", "h3"])
            if not heading:
                continue
            name = self._clean_text(heading.get_text()).lower()
            text = self._clean_text(sec.get_text())
            if text:
                sections[name] = text

        # Fallback
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

    def extract_tables(self, soup) -> list[ParsedTable]:
        tables = []
        for i, table_el in enumerate(soup.find_all("table"), 1):
            caption_el = table_el.find("caption") or table_el.find_previous("header", class_="article-table-caption")
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

            img_url = img.get("src", "") or img.get("data-src", "")
            if img_url and not img_url.startswith("http"):
                img_url = urljoin(base_url, img_url)

            caption_el = fig_el.find("figcaption")
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Figure {i}"

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=img_url,
            ))
        return figures
