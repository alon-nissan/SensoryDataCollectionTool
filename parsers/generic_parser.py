#!/usr/bin/env python3
"""Generic fallback HTML parser for publishers without a dedicated parser."""

from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class GenericParser(BaseParser):
    """Best-effort HTML parser for arbitrary publisher formats."""

    publisher_name = "generic"

    # Common section heading patterns
    SECTION_PATTERNS = {
        "introduction": ["introduction", "background"],
        "methods": ["methods", "materials and methods", "experimental",
                     "experimental section", "methodology", "materials"],
        "results": ["results", "results and discussion", "findings"],
        "discussion": ["discussion", "general discussion"],
        "conclusion": ["conclusion", "conclusions", "concluding remarks"],
        "references": ["references", "bibliography"],
    }

    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        content = self._read_file(html_path)
        soup = BeautifulSoup(content, "lxml")

        # Title: try multiple patterns
        title = ""
        for selector in [
            ("h1", {"class": "title"}),
            ("h1", {}),
            ("meta", {"name": "citation_title"}),
            ("meta", {"property": "og:title"}),
        ]:
            el = soup.find(selector[0], selector[1])
            if el:
                title = el.get("content", "") or self._clean_text(el.get_text())
                if title:
                    break

        # Abstract
        abstract = ""
        for selector in [
            ("div", {"class": "abstract"}),
            ("section", {"id": "abstract"}),
            ("div", {"id": "abstract"}),
            ("p", {"class": "abstract"}),
        ]:
            el = soup.find(selector[0], selector[1])
            if el:
                abstract = self._clean_text(el.get_text())
                break

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
            parse_confidence=0.7,  # Lower confidence for generic parser
        )

        if not sections:
            article.parse_warnings.append("No sections found with generic parser")
            article.parse_confidence *= 0.5
        if not tables:
            article.parse_warnings.append("No tables found")

        return article

    def extract_sections(self, soup) -> dict[str, str]:
        sections = {}

        # Strategy 1: <section> elements
        for sec in soup.find_all("section"):
            heading = sec.find(["h2", "h3", "h4"])
            if heading:
                name = self._clean_text(heading.get_text()).lower()
                text = self._clean_text(sec.get_text().replace(heading.get_text(), "", 1))
                if text:
                    canonical = self._canonicalize_section(name)
                    sections[canonical or name] = text

        if sections:
            return sections

        # Strategy 2: heading-based extraction
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

    def _canonicalize_section(self, name: str) -> str | None:
        """Map a section heading to a canonical name."""
        name_lower = name.lower().strip()
        # Remove leading numbers/dots (e.g., "2. Methods" → "methods")
        import re
        name_lower = re.sub(r'^\d+\.?\s*', '', name_lower)

        for canonical, patterns in self.SECTION_PATTERNS.items():
            for pattern in patterns:
                if pattern in name_lower:
                    return canonical
        return None

    def extract_tables(self, soup) -> list[ParsedTable]:
        tables = []
        for i, table_el in enumerate(soup.find_all("table"), 1):
            # Look for caption in various places
            caption = f"Table {i}"
            for cap_search in [
                table_el.find("caption"),
                table_el.find_previous(string=lambda s: s and f"table {i}" in s.lower()),
            ]:
                if cap_search:
                    caption = self._clean_text(
                        cap_search.get_text() if hasattr(cap_search, 'get_text') else str(cap_search)
                    )
                    break

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
        seen_urls = set()

        # Strategy 1: <figure> elements
        for i, fig in enumerate(soup.find_all("figure"), 1):
            img = fig.find("img")
            if not img:
                continue
            url = img.get("src", "") or img.get("data-src", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            if url and not url.startswith("http"):
                url = urljoin(base_url, url)

            caption_el = fig.find("figcaption")
            caption = self._clean_text(caption_el.get_text()) if caption_el else f"Figure {i}"

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=url,
            ))

        # Strategy 2: standalone <img> tags near figure captions (if no <figure> found)
        if not figures:
            for i, img in enumerate(soup.find_all("img"), 1):
                url = img.get("src", "") or img.get("data-src", "")
                if not url or url in seen_urls:
                    continue
                # Skip tiny images (likely icons)
                width = img.get("width", "999")
                try:
                    if int(width) < 100:
                        continue
                except (ValueError, TypeError):
                    pass

                seen_urls.add(url)
                if not url.startswith("http"):
                    url = urljoin(base_url, url)

                figures.append(ParsedFigure(
                    figure_id=f"figure_{i}",
                    caption=img.get("alt", f"Figure {i}"),
                    image_url=url,
                ))

        return figures
