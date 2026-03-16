#!/usr/bin/env python3
"""
Enhanced HTML parser for scientific articles from any publisher.

Consolidates extraction patterns from publisher-specific parsers (Elsevier,
Springer, Wiley, MDPI, OUP) into a single robust parser with multi-strategy
fallbacks for titles, abstracts, sections, tables, and figures.

Output structure (ParsedArticle):
├── study_id, doi, publisher, source_path, source_type
├── title: str              — article title
├── abstract: str           — article abstract
├── sections: dict          — {canonical_name: text}
│   Expected keys: introduction, methods, results, discussion, conclusion, references
│   (non-canonical section names preserved as-is)
├── tables: list[ParsedTable]
│   └── table_id, caption, headers: list[str], rows: list[dict], raw_html
├── figures: list[ParsedFigure]
│   └── figure_id, caption, image_url, local_path, surrounding_text
├── full_text: str          — concatenated markdown of title + abstract + sections
├── parse_confidence: float — 0.0–1.0 quality indicator
└── parse_warnings: list[str]
"""

import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parsers.base_parser import BaseParser, ParsedArticle, ParsedFigure, ParsedTable


class GenericParser(BaseParser):
    """Robust HTML/XML parser combining patterns from all major publishers."""

    publisher_name = "html"

    SECTION_PATTERNS = {
        "introduction": ["introduction", "background"],
        "methods": ["methods", "materials and methods", "experimental",
                     "experimental section", "methodology", "materials",
                     "experimental procedures"],
        "results": ["results", "results and discussion", "findings"],
        "discussion": ["discussion", "general discussion"],
        "conclusion": ["conclusion", "conclusions", "concluding remarks"],
        "references": ["references", "bibliography"],
    }

    # Comprehensive title selectors (publisher-specific + generic), tried in order
    _TITLE_SELECTORS = [
        ("h1", {"class_": "wi-article-title"}),       # OUP
        ("h1", {"class_": "article-title-main"}),      # OUP alt
        ("h1", {"class_": "at-articleTitle"}),          # OUP alt
        ("span", {"class_": "title-text"}),             # Elsevier
        ("h1", {"class_": "c-article-title"}),          # Springer
        ("h1", {"class_": "citation__title"}),          # Wiley
        ("span", {"class_": "article-header__title"}),  # Wiley alt
        ("h1", {"class_": "title"}),                    # MDPI
        ("h1", {}),                                     # Generic
    ]

    # Comprehensive abstract selectors, tried in order
    _ABSTRACT_SELECTORS = [
        ("section", {"class_": "abstract"}),             # OUP
        ("div", {"class_": "abstract"}),                 # Generic / Elsevier
        ("div", {"id": "abstracts"}),                    # Elsevier alt
        ("div", {"class_": "art-abstract"}),             # MDPI
        ("div", {"class_": "article-section__abstract"}),  # Wiley
        ("section", {"class_": "article-section--abstract"}),  # Wiley alt
        ("section", {"id": "abstract"}),                 # Generic
        ("div", {"id": "abstract"}),                     # Generic
        ("p", {"class_": "abstract"}),                   # Generic
        ("p", {"class_": "chapter-para"}),               # OUP older
    ]

    # Article body selectors — used to scope table/figure search to main content
    _ARTICLE_BODY_SELECTORS = [
        ("div", {"class_": "article-body"}),      # OUP
        ("div", {"id": "body"}),                   # Elsevier ScienceDirect
        ("article", {}),                           # Springer, MDPI, generic HTML5
        ("div", {"class_": "c-article-body"}),     # Springer Nature
        ("main", {}),                              # HTML5 fallback
    ]

    # Elsevier XML namespaces
    _ELSEVIER_NS = {
        "ce": "http://www.elsevier.com/xml/common/dtd",
        "ja": "http://www.elsevier.com/xml/ja/dtd",
        "xlink": "http://www.w3.org/1999/xlink",
    }

    def parse(self, html_path: Path, doi: str = "", study_id: str = "") -> ParsedArticle:
        content = self._read_file(html_path)

        # Detect XML vs HTML
        is_xml = content.strip().startswith("<?xml") or "<ce:" in content
        if not is_xml and ("<article" in content[:500] and "dtd-version" in content[:2000]):
            is_xml = True

        parser_name = "lxml-xml" if is_xml else "lxml"
        soup = BeautifulSoup(content, parser_name)

        if is_xml:
            return self._parse_xml(soup, html_path, doi, study_id)
        return self._parse_html(soup, html_path, doi, study_id)

    def _parse_html(self, soup, html_path, doi, study_id) -> ParsedArticle:
        """Parse HTML from any publisher."""
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
            article.parse_warnings.append("No sections found")
            article.parse_confidence *= 0.5
        if not tables:
            article.parse_warnings.append("No tables found")

        return article

    def _parse_xml(self, soup, html_path, doi, study_id) -> ParsedArticle:
        """Parse XML (JATS / Elsevier XML)."""
        # Title
        title_el = (
            soup.find("ce:title") or soup.find("dc:title")
            or soup.find("article-title") or soup.find("title")
        )
        title = self._clean_text(title_el.get_text()) if title_el else ""

        # Abstract
        abstract_el = (
            soup.find("ce:abstract") or soup.find("dc:description")
            or soup.find("abstract")
        )
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
            parse_confidence=0.8,
        )

        if not sections:
            article.parse_warnings.append("No sections found in XML")
            article.parse_confidence *= 0.5

        return article

    # ── Title extraction ────────────────────────────────────────

    def _extract_title(self, soup) -> str:
        for tag, attrs in self._TITLE_SELECTORS:
            el = soup.find(tag, **attrs)
            if el:
                title = self._clean_text(el.get_text())
                if title:
                    return title

        # Fallback: meta tags
        for meta_name in ["citation_title", "og:title", "DC.title"]:
            el = soup.find("meta", attrs={"name": meta_name}) or soup.find("meta", attrs={"property": meta_name})
            if el and el.get("content"):
                return el["content"].strip()

        return ""

    # ── Abstract extraction ─────────────────────────────────────

    def _extract_abstract(self, soup) -> str:
        for tag, attrs in self._ABSTRACT_SELECTORS:
            el = soup.find(tag, **attrs)
            if el:
                text = self._clean_text(el.get_text())
                if text:
                    return text

        # Fallback: Springer-style abstract by ID substring
        el = soup.find("div", id=lambda x: x and "abstract" in str(x).lower())
        if el:
            return self._clean_text(el.get_text())

        return ""

    # ── Section extraction ──────────────────────────────────────

    def extract_sections(self, soup) -> dict[str, str]:
        # Strategy 1: <section> or <div class="section"> containers
        sections = self._extract_sections_from_containers(soup)
        if sections:
            return sections

        # Strategy 2: Publisher-specific containers
        sections = self._extract_sections_publisher_specific(soup)
        if sections:
            return sections

        # Strategy 3: Heading-based fallback (h2 siblings)
        return self._extract_sections_by_headings(soup)

    def _extract_sections_from_containers(self, soup) -> dict[str, str]:
        """Strategy 1: Extract from <section> elements with headings."""
        sections = {}

        # Try <section> elements first, then <div class="section">
        containers = soup.find_all("section")
        if not containers:
            containers = soup.find_all("div", class_="section")

        for sec in containers:
            heading = sec.find(["h2", "h3", "h4"])
            if not heading:
                continue
            name = self._clean_text(heading.get_text()).lower()
            # Get text either from paragraphs (more precise) or full section
            paras = sec.find_all("p")
            if paras:
                text = " ".join(self._clean_text(p.get_text()) for p in paras)
            else:
                text = self._clean_text(sec.get_text().replace(heading.get_text(), "", 1))
            if text and len(text) > 20:
                canonical = self._canonicalize_section(name)
                key = canonical or name
                if key in sections:
                    sections[key] += " " + text
                else:
                    sections[key] = text

        return sections

    def _extract_sections_publisher_specific(self, soup) -> dict[str, str]:
        """Strategy 2: Publisher-specific section containers."""
        sections = {}

        # Elsevier: div.section-paragraph
        for sec_el in soup.find_all("div", class_="section-paragraph"):
            heading = sec_el.find_previous(["h2", "h3"])
            if not heading:
                continue
            name = self._clean_text(heading.get_text()).lower()
            text = self._clean_text(sec_el.get_text())
            if text:
                canonical = self._canonicalize_section(name)
                key = canonical or name
                sections[key] = sections.get(key, "") + " " + text

        if sections:
            return {k: v.strip() for k, v in sections.items()}

        # Wiley: section.article-section__content
        for sec_el in soup.find_all("section", class_="article-section__content"):
            heading = sec_el.find_previous(["h2", "h3"])
            if not heading:
                continue
            name = self._clean_text(heading.get_text()).lower()
            text = self._clean_text(sec_el.get_text())
            if text:
                canonical = self._canonicalize_section(name)
                sections[canonical or name] = text

        if sections:
            return sections

        # OUP: div.article-body > children
        body = soup.find("div", class_="article-body")
        if body:
            for child in body.find_all(["section", "div"], recursive=False):
                heading = child.find(["h2", "h3", "h4"])
                if not heading:
                    continue
                name = self._clean_text(heading.get_text()).lower()
                text = self._clean_text(child.get_text().replace(heading.get_text(), "", 1))
                if text and len(text) > 20:
                    canonical = self._canonicalize_section(name)
                    sections[canonical or name] = text

        return sections

    def _extract_sections_by_headings(self, soup) -> dict[str, str]:
        """Strategy 3: Heading-based extraction (collect siblings between h2 tags)."""
        sections = {}
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
        name_lower = re.sub(r'^\d+\.?\s*', '', name.lower().strip())
        for canonical, patterns in self.SECTION_PATTERNS.items():
            for pattern in patterns:
                if pattern in name_lower:
                    return canonical
        return None

    # ── XML section extraction ──────────────────────────────────

    def _extract_xml_sections(self, soup) -> dict[str, str]:
        sections = {}

        # Elsevier XML: ce:section
        for sec in soup.find_all("ce:section"):
            title_el = sec.find("ce:section-title")
            if not title_el:
                continue
            name = self._clean_text(title_el.get_text()).lower()
            paras = sec.find_all("ce:para")
            text = " ".join(self._clean_text(p.get_text()) for p in paras)
            if text:
                sections[name] = text

        if sections:
            return sections

        # JATS XML: <sec> elements
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

    # ── Article body scoping ───────────────────────────────────

    def _find_article_body(self, soup):
        """Return the article content container, or soup itself as fallback."""
        for tag, attrs in self._ARTICLE_BODY_SELECTORS:
            el = soup.find(tag, **attrs)
            if el:
                return el
        return soup

    # ── Table extraction ────────────────────────────────────────

    def extract_tables(self, soup) -> list[ParsedTable]:
        root = self._find_article_body(soup)
        tables = []
        seen_fingerprints: set[tuple] = set()

        for table_el in root.find_all("table"):
            headers, rows = self._parse_html_table(table_el)
            if not headers:
                continue

            # Content-hash dedup: fingerprint from headers + first 3 rows
            row_vals = tuple(
                tuple(r.get(h, "") for h in headers)
                for r in rows[:3]
            )
            fingerprint = (tuple(headers), row_vals)
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)

            idx = len(tables) + 1
            caption = self._find_table_caption(table_el, idx)
            tables.append(ParsedTable(
                table_id=f"table_{idx}",
                caption=caption,
                headers=headers,
                rows=rows,
                raw_html=str(table_el),
            ))
        return tables

    def _find_table_caption(self, table_el, index: int) -> str:
        """Search for table caption using multiple strategies."""
        # 1. <caption> element inside table
        cap = table_el.find("caption")
        if cap:
            return self._clean_text(cap.get_text())

        # 2. Parent wrapper with "table" in class → child with "caption" in class (OUP)
        parent = table_el.find_parent(
            ["div", "figure"], class_=lambda c: c and "table" in str(c)
        )
        if parent:
            cap = parent.find(class_=lambda c: c and "caption" in str(c))
            if cap:
                return self._clean_text(cap.get_text())

        # 3. Preceding div.table-caption (MDPI / Elsevier)
        for cls in ["table-caption", "c-article-table-caption", "article-table-caption"]:
            cap = table_el.find_previous("div", class_=cls) or table_el.find_previous("header", class_=cls)
            if cap:
                return self._clean_text(cap.get_text())

        # 4. Text search for "Table N" nearby
        cap = table_el.find_previous(
            string=lambda s: s and f"table {index}" in s.lower()
        )
        if cap:
            text = self._clean_text(str(cap))
            if text:
                return text

        return f"Table {index}"

    def _extract_xml_tables(self, soup) -> list[ParsedTable]:
        tables = []
        # Elsevier ce:table
        xml_tables = soup.find_all("ce:table") or soup.find_all("table-wrap")
        for i, table_el in enumerate(xml_tables, 1):
            cap_el = table_el.find("ce:caption") or table_el.find("caption") or table_el.find("label")
            caption = self._clean_text(cap_el.get_text()) if cap_el else f"Table {i}"
            inner = table_el.find("table")
            if inner:
                headers, rows = self._parse_html_table(inner)
                if headers:
                    tables.append(ParsedTable(
                        table_id=f"table_{i}",
                        caption=caption,
                        headers=headers,
                        rows=rows,
                        raw_html=str(table_el),
                    ))
        return tables

    # ── Figure extraction ───────────────────────────────────────

    # Classes that indicate non-figure images (graphical abstracts, overlays)
    _SKIP_FIGURE_CLASSES = {"abstract", "graphical-abstract", "toc"}

    def extract_figures(self, soup, base_url: str = "") -> list[ParsedFigure]:
        figures = []
        seen_urls = set()

        # Strategy 1: <figure> elements AND <div class*="fig"> containers
        fig_containers = soup.find_all("figure")
        fig_divs = soup.find_all(
            "div", class_=lambda c: c and "fig" in str(c).lower()
        )
        # Merge, skipping containers that are descendants of already-added ones
        # or that represent non-figure content (graphical abstracts, hidden popups)
        all_containers = list(fig_containers)
        for d in fig_divs:
            classes_str = " ".join(d.get("class", [])).lower()
            # Skip graphical abstract / TOC containers
            if any(skip in classes_str for skip in self._SKIP_FIGURE_CLASSES):
                continue
            # Skip if descendant of any container already collected
            if any(d in c.descendants for c in all_containers):
                continue
            all_containers.append(d)

        fig_count = 0
        for fig in all_containers:
            img = fig.find("img")
            if not img:
                continue

            url = img.get("src", "") or img.get("data-src", "")
            if not url:
                continue

            # Deduplicate by exact URL
            if url in seen_urls:
                continue

            # Deduplicate multi-resolution variants of the same figure.
            # Publishers serve thumbnails (-550.jpg) alongside full-res (.png).
            # Normalize: strip resolution suffixes and extension, then compare.
            url_key = self._normalize_figure_url(url)
            existing_idx = None
            for idx, (existing_fig, existing_key) in enumerate(
                [(f, self._normalize_figure_url(f.image_url)) for f in figures]
            ):
                if url_key and existing_key and url_key == existing_key:
                    existing_idx = idx
                    break

            if existing_idx is not None:
                # Prefer higher-resolution version (larger file name = no -550 suffix)
                if "-550" not in url and "-550" in figures[existing_idx].image_url:
                    old_url = figures[existing_idx].image_url
                    figures[existing_idx] = ParsedFigure(
                        figure_id=figures[existing_idx].figure_id,
                        caption=figures[existing_idx].caption,
                        image_url=url if not url.startswith("http") and base_url
                                  else (urljoin(base_url, url) if base_url and not url.startswith("http") else url),
                        surrounding_text=figures[existing_idx].surrounding_text,
                    )
                    seen_urls.add(url)
                continue

            seen_urls.add(url)

            if url and not url.startswith("http"):
                url = urljoin(base_url, url) if base_url else url

            # Caption: try figcaption, then child with "caption"/"description" class, then alt text
            caption = ""
            cap_el = fig.find("figcaption")
            if not cap_el:
                cap_el = fig.find(class_=lambda c: c and (
                    "caption" in str(c).lower() or "description" in str(c).lower()
                ))
            if cap_el:
                caption = self._clean_text(cap_el.get_text())
            if not caption:
                caption = img.get("alt", "")

            fig_count += 1
            if not caption:
                caption = f"Figure {fig_count}"

            # Surrounding text for context
            prev_p = fig.find_previous("p")
            surrounding = self._clean_text(prev_p.get_text())[:500] if prev_p else ""

            figures.append(ParsedFigure(
                figure_id=f"figure_{fig_count}",
                caption=caption,
                image_url=url,
                surrounding_text=surrounding,
            ))

        # Strategy 2: Standalone <img> tags (if no figures found via containers)
        if not figures:
            for img in soup.find_all("img"):
                url = img.get("src", "") or img.get("data-src", "")
                if not url or url in seen_urls:
                    continue
                # Skip tiny images (likely icons/logos)
                width = img.get("width", "999")
                try:
                    if int(width) < 100:
                        continue
                except (ValueError, TypeError):
                    pass

                seen_urls.add(url)
                if not url.startswith("http"):
                    url = urljoin(base_url, url) if base_url else url

                fig_count += 1
                figures.append(ParsedFigure(
                    figure_id=f"figure_{fig_count}",
                    caption=img.get("alt", f"Figure {fig_count}"),
                    image_url=url,
                ))

        return figures

    @staticmethod
    def _normalize_figure_url(url: str) -> str:
        """Normalize a figure URL to detect multi-resolution duplicates.

        Strips resolution suffixes (e.g., '-550') and file extensions so that
        'nutrients-10-01632-g001-550.jpg' and 'nutrients-10-01632-g001.png'
        both normalize to 'nutrients-10-01632-g001'.
        """
        # Get just the filename
        name = url.rsplit("/", 1)[-1] if "/" in url else url
        # Strip extension
        name = name.rsplit(".", 1)[0] if "." in name else name
        # Strip common resolution suffixes: -550, -1024, _small, _large, etc.
        name = re.sub(r'[-_]\d{3,4}$', '', name)
        name = re.sub(r'[-_](small|large|thumb|preview|hi-res|hires)$', '', name, flags=re.IGNORECASE)
        return name.lower()

    def _extract_xml_figures(self, soup) -> list[ParsedFigure]:
        figures = []
        # Elsevier ce:figure or JATS fig
        xml_figs = soup.find_all("ce:figure") or soup.find_all("fig")
        for i, fig_el in enumerate(xml_figs, 1):
            cap_el = fig_el.find("ce:caption") or fig_el.find("caption") or fig_el.find("label")
            caption = self._clean_text(cap_el.get_text()) if cap_el else f"Figure {i}"

            graphic = (
                fig_el.find("ce:e-component") or fig_el.find("graphic")
                or fig_el.find("img")
            )
            img_url = ""
            if graphic:
                img_url = (
                    graphic.get("xlink:href", "")
                    or graphic.get("src", "")
                    or graphic.get("id", "")
                )

            figures.append(ParsedFigure(
                figure_id=f"figure_{i}",
                caption=caption,
                image_url=img_url,
            ))
        return figures
