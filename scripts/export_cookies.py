#!/usr/bin/env python3
"""Export authentication cookies from the running Chrome browser to disk.

Use this when you're already logged in to a publisher via your HUJI Chrome profile.
The saved cookies are then used automatically by the fetch pipeline (Layer 3).

Usage:
    python scripts/export_cookies.py            # exports OUP cookies (default)
    python scripts/export_cookies.py --publisher wiley
    python scripts/export_cookies.py --publisher oup wiley

Steps:
    1. Make sure you are logged in to the publisher in Chrome (HUJI profile)
    2. Run this script — it connects to Chrome via remote debugging
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
    CHROME_APP, CHROME_DEBUG_PORT, CHROME_PROFILE_DIR, CHROME_USER_DATA_DIR,
    COOKIE_DIR, COOKIE_TTL_SECONDS, ShibbolethSession,
)

# Publisher → URL to navigate to (so the browser has that domain's cookies loaded)
PUBLISHER_TEST_URLS = {
    "oup": "https://academic.oup.com",
    "wiley": "https://onlinelibrary.wiley.com",
    "elsevier": "https://www.sciencedirect.com",
    "springer": "https://link.springer.com",
}


def export_cookies(publisher: str) -> bool:
    """Connect to Chrome, navigate to the publisher, and save cookies."""
    session = ShibbolethSession()

    print(f"\n🍪 Exporting cookies for: {publisher}")
    print("  Connecting to Chrome (HUJI profile)...")

    try:
        session._ensure_driver()
    except RuntimeError as e:
        print(f"  ❌ Could not connect to Chrome: {e}")
        print("     Make sure Chrome is open with your HUJI profile.")
        return False

    test_url = PUBLISHER_TEST_URLS.get(publisher, f"https://{publisher}.com")
    print(f"  🌐 Navigating to {test_url} to load domain cookies...")
    session.driver.get(test_url)
    time.sleep(3)

    # Check if we're actually logged in
    current_url = session.driver.current_url
    page_src = session.driver.page_source.lower()
    logged_in_hints = ["my account", "sign out", "log out", "welcome", "profile"]
    appears_logged_in = any(hint in page_src for hint in logged_in_hints)

    if appears_logged_in:
        print(f"  ✅ Detected active session at {current_url}")
    else:
        print(f"  ⚠ Could not confirm active session — saving cookies anyway")
        print(f"     If you're not logged in, run: python scripts/export_cookies.py after logging in")

    # Save cookies
    session.save_cookies(publisher)

    # Show cookie summary
    cookie_path = COOKIE_DIR / f"{publisher}_cookies.json"
    if cookie_path.exists():
        data = json.loads(cookie_path.read_text())
        cookies = data.get("cookies", [])
        auth_cookies = [c for c in cookies if any(
            k in c.get("name", "").lower()
            for k in ["session", "auth", "token", "login", "shibboleth", "openathens", "access"]
        )]
        print(f"  📦 Total cookies saved: {len(cookies)}")
        print(f"  🔑 Auth-related cookies: {len(auth_cookies)}")
        if auth_cookies:
            print(f"     Names: {', '.join(c['name'] for c in auth_cookies[:5])}")
        ttl_hours = COOKIE_TTL_SECONDS / 3600
        print(f"  ⏱ Cookies valid for: {ttl_hours:.0f} hours from now")
        print(f"  📁 Saved to: {cookie_path}")
    else:
        print("  ❌ Cookie file was not created")
        return False

    # Keep Chrome connected (don't call session.close())
    session.driver.quit()  # disconnects Selenium but leaves Chrome running
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Export authentication cookies from Chrome to the pipeline cookie cache.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/export_cookies.py                      # OUP (default)
  python scripts/export_cookies.py --publisher wiley
  python scripts/export_cookies.py --publisher oup wiley
        """,
    )
    parser.add_argument(
        "--publisher",
        nargs="+",
        default=["oup"],
        choices=list(PUBLISHER_TEST_URLS.keys()),
        help="Publisher(s) to export cookies for (default: oup)",
    )
    args = parser.parse_args()

    any_failed = False
    for pub in args.publisher:
        ok = export_cookies(pub)
        if not ok:
            any_failed = True

    if any_failed:
        print("\n⚠ Some exports failed. Check Chrome is open with your HUJI profile.")
        sys.exit(1)
    else:
        print("\n✅ Done! The pipeline will now use saved cookies automatically.")
        print("   Run: python scripts/orchestrate.py --doi \"10.xxxx/yyyy\"")


if __name__ == "__main__":
    main()
