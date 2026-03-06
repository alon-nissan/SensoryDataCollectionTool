#!/usr/bin/env python3
"""Parser for Oxford University Press (OUP) HTML articles."""

from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class OUPParser(BaseParser):
    """Parser for OUP journal articles (e.g., Chemical Senses)."""

    publisher_name = "oup"
    BASE_URL = "https://academic.oup.com"

    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        content = self._read_file(html_path)
        soup = BeautifulSoup(content, "lxml")

        title = self._extract_title(soup)
        abstract = self._extract_abstract(soup)
        sections = self.extract_sections(soup)
        tables = self.extract_tables(soup)
        figures = self.extract_figures(soup)

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
            parse_confidence=0.85,
        )

        if not sections:
            article.parse_warnings.append("No sections found in OUP HTML")
            article.parse_confidence *= 0.6
        if not tables:
            article.parse_warnings.append("No tables found")

        return article

    def _extract_title(self, soup) -> str:
        """Extract article title from OUP HTML."""
        # Modern OUP: <h1 class="wi-article-title">
        for selector in [
            ("h1", {"class": "wi-article-title"}),
            ("h1", {"class": "article-title-main"}),
            ("h1", {"class": "at-articleTitle"}),
            ("h1", {}),
            ("meta", {"name": "citation_title"}),
        ]:
            el = soup.find(selector[0], selector[1])
            if el:
                return el.get("content", "") or self._clean_text(el.get_text())
        return ""

    def _extract_abstract(self, soup) -> str:
        """Extract abstract from OUP HTML."""
        for selector in [
            ("section", {"class": "abstract"}),
            ("div", {"class": "abstract"}),
            ("div", {"id": "abstract"}),
            ("section", {"id": "abstract"}),
            ("p", {"class": "chapter-para"}),  # Older OUP
        ]:
            el = soup.find(selector[0], selector[1])
            if el:
                return self._clean_text(el.get_text())
        return ""

    def extract_sections(self, soup) -> dict[str, str]:
        """Extract named sections from OUP article HTML."""
        sections = {}

        # Modern OUP: <div class="section"> or <section> with <h2>
        section_containers = soup.find_all(["section", "div"], class_="section")
        if not section_containers:
            section_containers = soup.find_all("div", class_="article-body")
            if section_containers:
                section_containers = section_containers[0].find_all(
                    ["section", "div"], recursive=False
                )

        for sec in section_containers:
            heading = sec.find(["h2", "h3", "h4"])
            if not heading:
                continue
            name = self._clean_text(heading.get_text()).lower()
            text = self._clean_text(sec.get_text().replace(heading.get_text(), "", 1))
            if text and len(text) > 20:
                canonical = self._canonicalize_section(name)
                sections[canonical or name] = text

        if sections:
            return sections

        # Fallback: heading-based extraction (older OUP HTML)
        for heading in soup.find_all("h2"):
            name = self._clean_text(heading.get_text()).lower()
            parts = []
            for sib in heading.find_next_siblings():
                if sib.name in ("h2", "h1"):
                    break
                text = self._clean_text(sib.get_text())
                if text:
                    parts.append(text)
            if parts:
                canonical = self._canonicalize_section(name)
                sections[canonical or name] = " ".join(parts)

        return sections

    SECTION_PATTERNS = {
        "introduction": ["introduction", "background"],
        "methods": ["methods", "materials and methods", "experimental",
                     "experimental section", "methodology", "materials"],
        "results": ["results", "results and discussion", "findings"],
        "discussion": ["discussion", "general discussion"],
        "conclusion": ["conclusion", "conclusions", "concluding remarks"],
        "references": ["references", "bibliography"],
    }

    def _canonicalize_section(self, name: str) -> str | None:
        """Map a section heading to a canonical name."""
        import re
        name_lower = re.sub(r'^\d+\.?\s*', '', name.lower().strip())
        for canonical, patterns in self.SECTION_PATTERNS.items():
            for pattern in patterns:
                if pattern in name_lower:
                    return canonical
        return None

    def extract_tables(self, soup) -> list[ParsedTable]:
        """Extract tables from OUP HTML."""
        tables = []

        for i, table_el in enumerate(soup.find_all("table"), 1):
            # OUP table captions: <div class="table-caption"> or <caption>
            caption = f"Table {i}"
            caption_el = table_el.find("caption")
            if not caption_el:
                # Look in parent wrapper
                parent = table_el.find_parent(["div", "figure"], class_=lambda c: c and "table" in str(c))
                if parent:
                    caption_el = parent.find(class_=lambda c: c and "caption" in str(c))

            if caption_el:
                caption = self._clean_text(caption_el.get_text())

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
        """Extract figures from OUP HTML."""
        figures = []
        seen_urls = set()
        base = base_url or self.BASE_URL

        # Modern OUP: <div class="fig"> or <figure>
        fig_containers = soup.find_all(["figure", "div"], class_=lambda c: c and "fig" in str(c))
        if not fig_containers:
            fig_containers = soup.find_all("figure")

        for i, fig in enumerate(fig_containers, 1):
            img = fig.find("img")
            if not img:
                continue

            url = img.get("src", "") or img.get("data-src", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            if not url.startswith("http"):
                url = urljoin(base, url)

            # Caption: <div class="fig-caption"> or <figcaption>
            caption = ""
            for cap_sel in [
                fig.find("figcaption"),
                fig.find(class_=lambda c: c and "caption" in str(c)),
            ]:
                if cap_sel:
                    caption = self._clean_text(cap_sel.get_text())
                    break
            if not caption:
                caption = img.get("alt", f"Figure {i}")

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=url,
            ))

        return figures
