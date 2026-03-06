#!/usr/bin/env python3
"""DOI resolver and article fetcher. Resolves DOI → publisher → downloads HTML/XML."""

import os
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


# Publisher detection patterns (domain → publisher key)
PUBLISHER_DOMAINS = {
    "mdpi.com": "mdpi",
    "elsevier.com": "elsevier",
    "sciencedirect.com": "elsevier",
    "springer.com": "springer",
    "springernature.com": "springer",
    "nature.com": "springer",
    "wiley.com": "wiley",
    "onlinelibrary.wiley.com": "wiley",
    "tandfonline.com": "taylor_francis",
    "frontiersin.org": "frontiers",
    "academic.oup.com": "oup",
    "oup.com": "oup",
}


def resolve_doi(doi: str) -> dict:
    """Resolve a DOI via CrossRef API to get publisher info and article URL.

    Returns dict with keys: doi, publisher, url, title, journal, year
    """
    # CrossRef API
    url = f"https://api.crossref.org/works/{doi}"
    headers = {"User-Agent": "SensoryExtraction/1.0 (mailto:research@university.edu)"}

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()["message"]

    # Detect publisher from URL or publisher name
    article_url = ""
    for link in data.get("link", []):
        if link.get("content-type") in ("text/html", "application/xml", "text/xml"):
            article_url = link["URL"]
            break
    if not article_url:
        article_url = data.get("URL", f"https://doi.org/{doi}")

    publisher_name = data.get("publisher", "").lower()
    publisher_key = _detect_publisher(article_url, publisher_name)

    return {
        "doi": doi,
        "publisher": publisher_key,
        "url": article_url,
        "title": data.get("title", [""])[0],
        "journal": data.get("container-title", [""])[0],
        "year": data.get("published-print", data.get("published-online", {}))
                     .get("date-parts", [[None]])[0][0],
    }


def _detect_publisher(url: str, publisher_name: str) -> str:
    """Detect publisher from URL domain or publisher name string."""
    url_lower = url.lower()
    for domain, key in PUBLISHER_DOMAINS.items():
        if domain in url_lower:
            return key

    # Fallback: match publisher name
    name_lower = publisher_name.lower()
    if "elsevier" in name_lower:
        return "elsevier"
    if "springer" in name_lower or "nature" in name_lower:
        return "springer"
    if "wiley" in name_lower:
        return "wiley"
    if "mdpi" in name_lower:
        return "mdpi"
    if "taylor" in name_lower or "francis" in name_lower:
        return "taylor_francis"
    if "oxford" in name_lower or "oup" in name_lower:
        return "oup"

    return "generic"


def fetch_html(doi: str, publisher: str, url: str, output_dir: Path, study_id: str = "") -> Path:
    """Fetch the article HTML/XML and save to disk.

    Returns the path to the saved file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = (study_id or doi.replace("/", "_")) + ".html"
    output_path = output_dir / filename

    if output_path.exists():
        print(f"  Already fetched: {output_path}")
        return output_path

    if publisher == "elsevier":
        content = _fetch_elsevier(doi)
        output_path = output_path.with_suffix(".xml")
    elif publisher == "springer":
        content = _fetch_springer(doi)
        output_path = output_path.with_suffix(".xml")
    elif publisher == "wiley":
        content = _fetch_wiley(doi, url)
    elif publisher == "oup":
        content = _fetch_oup(doi, url)
    else:
        content = _fetch_open_access(url)

    output_path.write_text(content, encoding="utf-8")
    print(f"  Saved: {output_path}")
    return output_path


def _fetch_open_access(url: str) -> str:
    """Fetch HTML from open-access publishers (MDPI, Frontiers, etc.)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SensoryExtraction/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _fetch_elsevier(doi: str) -> str:
    """Fetch article from Elsevier Article Retrieval API."""
    api_key = os.getenv("ELSEVIER_API_KEY")
    insttoken = os.getenv("ELSEVIER_INSTTOKEN", "")

    if not api_key:
        print("  ⚠ No ELSEVIER_API_KEY set. Trying direct HTML fetch...")
        return _fetch_open_access(f"https://www.sciencedirect.com/science/article/pii/{doi}")

    url = f"https://api.elsevier.com/content/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "text/xml",
    }
    if insttoken:
        headers["X-ELS-Insttoken"] = insttoken

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_springer(doi: str) -> str:
    """Fetch article from Springer Nature API."""
    api_key = os.getenv("SPRINGER_API_KEY")

    if not api_key:
        print("  ⚠ No SPRINGER_API_KEY set. Trying direct HTML fetch...")
        return _fetch_open_access(f"https://link.springer.com/article/{doi}")

    url = f"https://api.springernature.com/openaccess/jats/doi/{doi}"
    params = {"api_key": api_key}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_wiley(doi: str, url: str) -> str:
    """Fetch article from Wiley (TDM or direct HTML)."""
    token = os.getenv("WILEY_TDM_TOKEN")

    if token:
        headers = {
            "CR-Clickthrough-Client-Token": token,
            "Accept": "text/html",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.text

    print("  ⚠ No WILEY_TDM_TOKEN set. Trying direct HTML fetch...")
    return _fetch_open_access(url)


def _fetch_oup(doi: str, url: str) -> str:
    """Fetch article from OUP via direct access or Shibboleth institutional login.

    Falls back to CrossRef PDF link if Shibboleth login fails.
    """
    # Step 1: Try direct HTML fetch (works for open access OUP articles)
    try:
        html = _fetch_open_access(url)
        if len(html) > 5000 and "article-body" in html.lower():
            print("  ✅ OUP article fetched directly (open access)")
            return html
    except Exception:
        pass

    # Step 2: Try Shibboleth login via Selenium
    print("  🔑 OUP requires authentication. Attempting Shibboleth login...")
    try:
        from scripts.institutional_login import get_session

        # Construct the proper article URL for OUP
        article_url = url
        if "doi.org" in url:
            article_url = f"https://academic.oup.com/article-lookup/doi/{doi}"

        session = get_session(headless=True)
        html = session.fetch_authenticated_html(article_url, publisher="oup")

        if len(html) > 5000:
            print("  ✅ OUP article fetched via Shibboleth")
            return html
    except Exception as e:
        print(f"  ⚠ Shibboleth login failed: {e}")

    # Step 3: Try CrossRef PDF link as fallback
    print("  📥 Falling back to CrossRef PDF link...")
    try:
        crossref_url = f"https://api.crossref.org/works/{doi}"
        headers = {"User-Agent": "SensoryExtraction/1.0 (mailto:research@university.edu)"}
        resp = requests.get(crossref_url, headers=headers, timeout=30)
        data = resp.json()["message"]
        for link in data.get("link", []):
            if link.get("content-type") == "application/pdf":
                pdf_url = link["URL"]
                print(f"  📄 PDF available at: {pdf_url}")
                print("  ⚠ Download PDF manually and place in data/html/ as a .pdf file")
                break
    except Exception:
        pass

    raise RuntimeError(
        f"Could not fetch OUP article {doi}. Options:\n"
        f"  1. Set HUJI_EMAIL and HUJI_PASSWORD in .env for Shibboleth login\n"
        f"  2. Download HTML manually from browser and place in data/html/\n"
        f"  3. Download PDF and use PDF fallback parser"
    )


def fetch_article(doi: str, output_dir: Path = None, study_id: str = "") -> dict:
    """Full pipeline: resolve DOI → detect publisher → fetch HTML/XML.

    Returns dict with: doi, publisher, title, journal, year, html_path
    """
    config = load_config()
    if output_dir is None:
        output_dir = ROOT_DIR / config["paths"]["html_dir"]

    print(f"\n📄 Fetching: {doi}")

    # Step 1: Resolve DOI
    print("  Resolving DOI via CrossRef...")
    info = resolve_doi(doi)
    print(f"  Publisher: {info['publisher']} | {info['title'][:60]}...")

    # Step 2: Fetch HTML/XML
    print(f"  Downloading article...")
    html_path = fetch_html(
        doi=doi,
        publisher=info["publisher"],
        url=info["url"],
        output_dir=output_dir,
        study_id=study_id or info["doi"].replace("/", "_"),
    )

    info["html_path"] = str(html_path)
    return info


def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_article.py <DOI> [study_id]")
        print("Example: python fetch_article.py 10.3390/nu10111632 wee2018")
        sys.exit(1)

    doi = sys.argv[1]
    study_id = sys.argv[2] if len(sys.argv) > 2 else ""
    result = fetch_article(doi, study_id=study_id)

    print(f"\n✅ Article fetched:")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
