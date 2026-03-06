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


def _launch_chrome_with_debug() -> bool:
    """Gracefully quit any running Chrome, then relaunch with remote debugging.
    
    Auth cookies are stored persistently in the Chrome profile on disk,
    so they survive a Chrome restart.
    Returns True if the debug port opened successfully.
    """
    import socket
    import subprocess

    # Step 1: Gracefully quit Chrome via osascript (macOS)
    print("  🔧 Quitting Chrome gracefully...")
    subprocess.run(
        ["osascript", "-e", 'quit app "Google Chrome"'],
        capture_output=True,
    )
    # Wait up to 10 seconds for Chrome to fully exit
    for i in range(20):
        time.sleep(0.5)
        result = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, text=True)
        if not result.stdout.strip():
            break
    else:
        # Force kill if still running
        result = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, text=True)
        for pid in result.stdout.strip().split():
            subprocess.run(["kill", "-9", pid], capture_output=True)
        time.sleep(2)

    # Step 2: Launch Chrome with remote debugging
    print(f"  🚀 Launching Chrome (HUJI profile) with debug port {CHROME_DEBUG_PORT}...")
    subprocess.Popen(
        [
            CHROME_APP,
            f"--remote-debugging-port={CHROME_DEBUG_PORT}",
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            f"--profile-directory={CHROME_PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Step 3: Wait up to 30 seconds for the debug port to open
    print("  ⏳ Waiting for Chrome to start...", end="", flush=True)
    for _ in range(60):
        time.sleep(0.5)
        try:
            s = socket.create_connection(("127.0.0.1", CHROME_DEBUG_PORT), timeout=1)
            s.close()
            print(" ready!")
            return True
        except (ConnectionRefusedError, OSError):
            print(".", end="", flush=True)
    print()
    return False


def export_cookies(publisher: str) -> bool:
    """Connect to Chrome, navigate to the publisher, and save cookies."""
    print(f"\n🍪 Exporting cookies for: {publisher}")

    # Launch Chrome with debug port (auth cookies survive restart)
    if not _launch_chrome_with_debug():
        print("  ❌ Chrome debug port never opened.")
        print("     Try closing Chrome manually (Cmd+Q) and running again.")
        return False

    session = ShibbolethSession()
    print("  Connecting Selenium to Chrome...")
    try:
        session._ensure_driver()
    except RuntimeError as e:
        print(f"  ❌ Could not connect to Chrome: {e}")
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
