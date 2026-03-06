#!/usr/bin/env python3
"""Export authentication cookies from the running Chrome browser to disk.

Reads Chrome's cookie database directly from disk — no need to relaunch Chrome
or use Selenium. Works with your existing Chrome session.

Usage:
    python scripts/export_cookies.py            # exports OUP cookies (default)
    python scripts/export_cookies.py --publisher wiley
    python scripts/export_cookies.py --publisher oup wiley

Steps:
    1. Make sure you are logged in to the publisher in Chrome (HUJI profile)
    2. Run this script — it reads cookies directly from Chrome's profile on disk
    3. Cookies are saved to data/_cookies/{publisher}_cookies.json
    4. Run the pipeline normally — it will use saved cookies automatically
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.institutional_login import (
    CHROME_PROFILE_DIR, CHROME_USER_DATA_DIR,
    COOKIE_DIR, COOKIE_TTL_SECONDS,
)

# Publisher → domains to extract cookies for
PUBLISHER_DOMAINS = {
    "oup": ["academic.oup.com", "oup.com", "shibboleth.huji.ac.il",
            "login.huji.ac.il", "idp.huji.ac.il"],
    "wiley": ["onlinelibrary.wiley.com", "wiley.com"],
    "elsevier": ["sciencedirect.com", "elsevier.com"],
    "springer": ["link.springer.com", "springer.com", "springernature.com"],
}


def _read_chrome_cookies(domains: list[str]) -> list[dict]:
    """Read cookies for given domains from Chrome's profile using browser-cookie3."""
    try:
        import browser_cookie3
    except ImportError:
        raise ImportError("Run: pip install browser-cookie3")

    profile_path = Path(CHROME_USER_DATA_DIR) / CHROME_PROFILE_DIR

    all_cookies = []
    seen = set()

    for domain in domains:
        try:
            jar = browser_cookie3.chrome(
                domain_name=domain,
                cookie_file=str(profile_path / "Cookies"),
            )
            for c in jar:
                key = (c.name, c.domain)
                if key not in seen:
                    seen.add(key)
                    all_cookies.append({
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain,
                        "path": c.path,
                        "secure": c.secure,
                        "expiry": int(c.expires) if c.expires else None,
                    })
        except Exception:
            continue

    return all_cookies


def export_cookies(publisher: str) -> bool:
    """Read cookies for a publisher from Chrome's profile and save to disk."""
    print(f"\n🍪 Exporting cookies for: {publisher}")

    domains = PUBLISHER_DOMAINS.get(publisher, [publisher])
    print(f"  📂 Reading Chrome profile: {CHROME_PROFILE_DIR}")
    print(f"  🌐 Domains: {', '.join(domains)}")

    try:
        cookies = _read_chrome_cookies(domains)
    except ImportError as e:
        print(f"  ❌ {e}")
        return False
    except Exception as e:
        print(f"  ❌ Failed to read Chrome cookies: {e}")
        return False

    if not cookies:
        print("  ⚠ No cookies found for these domains.")
        print("     Make sure you are logged in to the publisher in your HUJI Chrome profile.")
        return False

    # Save to pipeline cookie cache
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_path = COOKIE_DIR / f"{publisher}_cookies.json"
    payload = {
        "timestamp": time.time(),
        "cookies": cookies,
    }
    cookie_path.write_text(json.dumps(payload, indent=2))

    # Summary
    auth_keywords = ["session", "auth", "token", "login", "shibboleth", "openathens",
                     "access", "idp", "shib"]
    auth_cookies = [c for c in cookies if any(k in c["name"].lower() for k in auth_keywords)]
    ttl_hours = COOKIE_TTL_SECONDS / 3600

    print(f"  📦 Total cookies saved: {len(cookies)}")
    print(f"  🔑 Auth-related cookies: {len(auth_cookies)}")
    if auth_cookies:
        print(f"     Names: {', '.join(c['name'] for c in auth_cookies[:8])}")
    print(f"  ⏱ Valid for: {ttl_hours:.0f} hours")
    print(f"  📁 Saved to: {cookie_path}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Export authentication cookies from Chrome to the pipeline cookie cache.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/export_cookies.py                        # OUP (default)
  python scripts/export_cookies.py --publisher wiley
  python scripts/export_cookies.py --publisher oup wiley
        """,
    )
    parser.add_argument(
        "--publisher",
        nargs="+",
        default=["oup"],
        choices=list(PUBLISHER_DOMAINS.keys()),
        help="Publisher(s) to export cookies for (default: oup)",
    )
    args = parser.parse_args()

    any_failed = False
    for pub in args.publisher:
        ok = export_cookies(pub)
        if not ok:
            any_failed = True

    if any_failed:
        print("\n⚠ Some exports failed. Make sure you're logged in in Chrome (HUJI profile).")
        sys.exit(1)
    else:
        print("\n✅ Done! The pipeline will use saved cookies automatically.")
        print("   Run: python scripts/orchestrate.py --doi \"10.xxxx/yyyy\"")


if __name__ == "__main__":
    main()

