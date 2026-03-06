#!/usr/bin/env python3
"""Springer Nature publisher parser."""

from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class SpringerParser(BaseParser):
    """Parser for Springer Nature articles (HTML/XML from API or web)."""

    publisher_name = "springer"

    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        content = self._read_file(html_path)

        is_xml = content.strip().startswith("<?xml") or "<article" in content[:500]
        parser = "lxml-xml" if is_xml else "lxml"
        soup = BeautifulSoup(content, parser)

        # Title
        title_el = (
            soup.find("h1", class_="c-article-title")
            or soup.find("article-title")
            or soup.find("h1")
        )
        title = self._clean_text(title_el.get_text()) if title_el else ""

        # Abstract
        abstract_el = (
            soup.find("div", class_="c-article-section__content", id=lambda x: x and "abstract" in str(x).lower())
            or soup.find("abstract")
            or soup.find("section", {"id": "Abs1"})
        )
        abstract = self._clean_text(abstract_el.get_text()) if abstract_el else ""

        sections = self.extract_sections(soup)
        tables = self.extract_tables(soup)
        figures = self.extract_figures(soup, base_url="https://link.springer.com")

        full_text = "\n\n".join(
            [f"# {title}", abstract] + [f"## {k}\n{v}" for k, v in sections.items()]
        )

        return ParsedArticle(
            study_id=study_id or html_path.stem,
            doi=doi,
            publisher=self.publisher_name,
            source_path=str(html_path),
            source_type="xml" if is_xml else "html",
            title=title,
            abstract=abstract,
            sections=sections,
            tables=tables,
            figures=figures,
            full_text=full_text,
        )

    def extract_sections(self, soup) -> dict[str, str]:
        sections = {}

        # Springer HTML: <section> with <h2> headings
        for sec in soup.find_all("section"):
            heading = sec.find(["h2", "h3"])
            if not heading:
                continue
            name = self._clean_text(heading.get_text()).lower()
            paras = sec.find_all("p")
            text = " ".join(self._clean_text(p.get_text()) for p in paras)
            if text:
                sections[name] = text

        # XML fallback: <sec> elements
        if not sections:
            for sec in soup.find_all("sec"):
                title_el = sec.find("title")
                if not title_el:
                    continue
                name = self._clean_text(title_el.get_text()).lower()
                paras = sec.find_all("p")
                text = " ".join(self._clean_text(p.get_text()) for p in paras)
                if text:
                    sections[name] = text

        return sections

    def extract_tables(self, soup) -> list[ParsedTable]:
        tables = []
        for i, table_el in enumerate(soup.find_all("table"), 1):
            caption_el = table_el.find("caption") or table_el.find_previous("div", class_="c-article-table-caption")
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
        for i, fig_el in enumerate(soup.find_all(["figure", "div"], class_=lambda c: c and "figure" in str(c).lower()), 1):
            img = fig_el.find("img")
            if not img:
                continue

            img_url = img.get("src", "") or img.get("data-src", "")
            if img_url and not img_url.startswith("http"):
                img_url = urljoin(base_url, img_url)

            caption_el = fig_el.find("figcaption") or fig_el.find("p", class_="c-article-figure-caption")
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Figure {i}"

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=img_url,
            ))
        return figures
