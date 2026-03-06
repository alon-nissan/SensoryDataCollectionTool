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
sys.path.insert(0, str(ROOT_DIR))
load_dotenv(ROOT_DIR / ".env")

from scripts.fetch_validation import validate_html, detect_access_status


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


def _is_vpn_active() -> bool:
    """Check if we appear to be on the HUJI network (e.g., Samba VPN active).

    Tests by making a HEAD request to a known paywalled OUP article URL and checking
    if we get a substantial response without being redirected to a login page.
    """
    test_url = "https://academic.oup.com/chemse/article/doi/10.1093/chemse/bjy075/5231387"
    try:
        resp = requests.head(
            test_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html",
            },
            timeout=10,
            allow_redirects=True,
        )
        # On VPN, we typically get a 200 with the article page
        # Off VPN, we may get redirected to a login/paywall page
        # Check final URL doesn't contain login indicators
        final_url = resp.url.lower()
        if resp.status_code == 200 and "login" not in final_url and "shibboleth" not in final_url:
            return True
    except Exception:
        pass
    return False


def _fetch_with_cookies(url: str, publisher: str) -> str | None:
    """Try fetching an article using saved authentication cookies."""
    try:
        from scripts.institutional_login import get_saved_cookies_for_requests
        cookies = get_saved_cookies_for_requests(publisher)
        if cookies is None:
            return None

        session = requests.Session()
        session.cookies.update(cookies)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })

        resp = session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        is_valid, issues = validate_html(resp.text, publisher)
        if is_valid:
            print(f"  ✅ Article fetched using saved cookies")
            return resp.text
        else:
            print(f"  ⚠ Cookies present but content invalid: {'; '.join(issues)}")
            return None
    except Exception as e:
        print(f"  ⚠ Cookie-based fetch failed: {e}")
        return None


def _fetch_unpaywall(doi: str) -> str | None:
    """Try to find and fetch an open-access version via Unpaywall API."""
    email = os.getenv("UNPAYWALL_EMAIL", "")
    if not email:
        return None

    try:
        url = f"https://api.unpaywall.org/v2/{doi}"
        resp = requests.get(url, params={"email": email}, timeout=15)
        if resp.status_code != 200:
            return None

        data = resp.json()

        # Look for the best open access location
        best_oa = data.get("best_oa_location")
        if not best_oa:
            oa_locations = data.get("oa_locations", [])
            if oa_locations:
                best_oa = oa_locations[0]

        if not best_oa:
            print("  ⚠ Unpaywall: no open-access version found")
            return None

        oa_url = best_oa.get("url_for_landing_page") or best_oa.get("url") or best_oa.get("url_for_pdf")
        if not oa_url:
            return None

        host_type = best_oa.get("host_type", "unknown")
        print(f"  🔓 Unpaywall found OA version ({host_type}): {oa_url}")

        # Fetch the open-access HTML
        html = _fetch_open_access(oa_url)
        if len(html) > 5000:
            print(f"  ✅ Fetched via Unpaywall ({host_type})")
            return html

    except Exception as e:
        print(f"  ⚠ Unpaywall lookup failed: {e}")

    return None


def _suggest_pdf_fallback(doi: str) -> None:
    """Check CrossRef for a PDF link and print instructions."""
    try:
        crossref_url = f"https://api.crossref.org/works/{doi}"
        headers = {"User-Agent": "SensoryExtraction/1.0 (mailto:research@university.edu)"}
        resp = requests.get(crossref_url, headers=headers, timeout=15)
        data = resp.json()["message"]
        for link in data.get("link", []):
            if link.get("content-type") == "application/pdf":
                pdf_url = link["URL"]
                print(f"  📄 PDF available at: {pdf_url}")
                print("  ⚠ Download PDF manually and place in data/html/ as a .pdf file")
                return
    except Exception:
        pass
    print("  ⚠ No PDF link found via CrossRef")


def _fetch_wiley(doi: str, url: str) -> str:
    """Fetch article from Wiley (TDM token, VPN, cookies, Unpaywall, or Shibboleth)."""
    # Try TDM token first (existing behavior)
    token = os.getenv("WILEY_TDM_TOKEN")
    if token:
        try:
            headers = {
                "CR-Clickthrough-Client-Token": token,
                "Accept": "text/html",
            }
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            is_valid, issues = validate_html(resp.text, "wiley")
            if is_valid:
                print("  ✅ Wiley article fetched via TDM token")
                return resp.text
            print(f"  ⚠ TDM fetch: content issues ({'; '.join(issues[:2])})")
        except Exception as e:
            print(f"  ⚠ TDM fetch failed: {e}")
    else:
        print("  ⚠ No WILEY_TDM_TOKEN set")

    # Try direct open access
    print("  🔍 Trying direct HTTP fetch...")
    try:
        html = _fetch_open_access(url)
        is_valid, _ = validate_html(html, "wiley")
        if is_valid:
            print("  ✅ Wiley article fetched directly (open access)")
            return html
    except Exception:
        pass

    # VPN
    if _is_vpn_active():
        print("  🌐 VPN detected! Fetching with institutional network access...")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            }
            resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            is_valid, _ = validate_html(resp.text, "wiley")
            if is_valid:
                print("  ✅ Wiley article fetched via VPN")
                return resp.text
        except Exception:
            pass

    # Saved cookies
    html = _fetch_with_cookies(url, "wiley")
    if html:
        return html

    # Unpaywall
    html = _fetch_unpaywall(doi)
    if html:
        return html

    # Shibboleth
    print("  🔑 Attempting Shibboleth login for Wiley...")
    try:
        from scripts.institutional_login import get_session
        session = get_session()
        html = session.fetch_authenticated_html(url, publisher="wiley")
        if len(html) > 5000:
            print("  ✅ Wiley article fetched via Shibboleth")
            return html
    except Exception as e:
        print(f"  ⚠ Shibboleth login failed: {e}")

    _suggest_pdf_fallback(doi)

    raise RuntimeError(
        f"Could not fetch Wiley article {doi}. All access methods failed.\n"
        f"  Options: Connect VPN, set WILEY_TDM_TOKEN, or download manually."
    )


def _fetch_oup(doi: str, url: str) -> str:
    """Fetch article from OUP via layered fallback strategy.

    Fallback chain:
      1. Direct HTTP (open access)
      2. VPN-aware HTTP (Samba VPN)
      3. Saved cookies
      4. Unpaywall (open access versions)
      5. Automated Shibboleth login
      6. PDF fallback (inform user)
    """
    article_url = url
    if "doi.org" in url:
        article_url = f"https://academic.oup.com/article-lookup/doi/{doi}"

    # Layer 1: Direct HTTP (open access)
    print("  🔍 Layer 1: Trying direct HTTP fetch...")
    try:
        html = _fetch_open_access(article_url)
        is_valid, issues = validate_html(html, "oup")
        if is_valid:
            print("  ✅ OUP article fetched directly (open access)")
            return html
        status = detect_access_status(html, "oup")
        print(f"  ⚠ Direct fetch: {status} ({'; '.join(issues[:2])})")
    except Exception as e:
        print(f"  ⚠ Direct fetch failed: {e}")

    # Layer 2: VPN-aware HTTP
    print("  🔍 Layer 2: Checking VPN access...")
    if _is_vpn_active():
        print("  🌐 VPN detected! Fetching with institutional network access...")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            }
            resp = requests.get(article_url, headers=headers, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            is_valid, issues = validate_html(resp.text, "oup")
            if is_valid:
                print("  ✅ OUP article fetched via VPN")
                return resp.text
            print(f"  ⚠ VPN fetch: content issues ({'; '.join(issues[:2])})")
        except Exception as e:
            print(f"  ⚠ VPN fetch failed: {e}")
    else:
        print("  ⚠ No VPN detected")

    # Layer 3: Saved cookies
    print("  🔍 Layer 3: Trying saved authentication cookies...")
    html = _fetch_with_cookies(article_url, "oup")
    if html:
        return html

    # Layer 4: Unpaywall
    print("  🔍 Layer 4: Checking Unpaywall for open-access version...")
    html = _fetch_unpaywall(doi)
    if html:
        return html

    # Layer 5: Automated Shibboleth login
    print("  🔍 Layer 5: Attempting automated Shibboleth login...")
    try:
        from scripts.institutional_login import get_session
        session = get_session()
        html = session.fetch_authenticated_html(article_url, publisher="oup")

        is_valid, issues = validate_html(html, "oup")
        if is_valid:
            print("  ✅ OUP article fetched via Shibboleth")
            return html

        # Even if validation has issues, save if content is substantial
        if len(html) > 5000:
            print(f"  ⚠ Shibboleth: content may be incomplete ({'; '.join(issues[:2])})")
            return html
    except Exception as e:
        print(f"  ⚠ Shibboleth login failed: {e}")

    # Layer 6: PDF fallback
    print("  🔍 Layer 6: Checking for PDF fallback...")
    _suggest_pdf_fallback(doi)

    raise RuntimeError(
        f"Could not fetch OUP article {doi}. All 6 access methods failed.\n"
        f"  Tried: direct HTTP → VPN → cookies → Unpaywall → Shibboleth → PDF\n"
        f"  Options:\n"
        f"    1. Connect to Samba VPN and retry\n"
        f"    2. Set HUJI_EMAIL and HUJI_PASSWORD in .env for Shibboleth login\n"
        f"    3. Set UNPAYWALL_EMAIL in .env for open-access fallback\n"
        f"    4. Download HTML manually from browser and place in data/html/\n"
        f"    5. Download PDF and use PDF fallback parser"
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
