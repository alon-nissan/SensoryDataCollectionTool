#!/usr/bin/env python3
"""Selenium-based institutional (Shibboleth/OpenAthens) login for accessing paywalled articles.

Strategy: Connect to the user's real Chrome browser via remote debugging port.
This bypasses Cloudflare bot detection since it IS a real browser session.

Usage:
  1. Close Chrome completely (Cmd+Q)
  2. The script will auto-launch Chrome with remote debugging enabled
  3. If Cloudflare CAPTCHA appears, solve it manually in the browser
  4. The script handles the rest (Shibboleth login, HTML extraction)
"""

import os
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DEBUG_DIR = ROOT_DIR / "data" / "_debug"
CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_DEBUG_PORT = 9222
CHROME_USER_DATA_DIR = str(Path.home() / "Library/Application Support/Google/Chrome")
CHROME_PROFILE_DIR = "Profile 3"  # HUJI account (alon.nissan1@mail.huji.ac.il)


class ShibbolethSession:
    """Automates Shibboleth/OpenAthens login using the user's real Chrome browser.

    Launches Chrome with remote debugging so Selenium controls the real browser
    with all its cookies, extensions, and fingerprints — bypassing Cloudflare.
    """

    INSTITUTION_NAME = "Hebrew University of Jerusalem"

    def __init__(self):
        self.driver = None
        self._chrome_process = None
        self._authenticated = False

    def _ensure_driver(self):
        """Launch Chrome with remote debugging and connect Selenium to it."""
        if self.driver is not None:
            return

        import socket
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        # Kill any existing Chrome instances that might be holding the port
        self._kill_existing_chrome()

        print(f"  🚀 Launching Chrome (HUJI profile) with remote debugging on port {CHROME_DEBUG_PORT}...")
        self._chrome_process = subprocess.Popen(
            [
                CHROME_APP,
                f"--remote-debugging-port={CHROME_DEBUG_PORT}",
                f"--user-data-dir={CHROME_USER_DATA_DIR}",
                f"--profile-directory={CHROME_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--no-startup-window",   # suppress window while we wait for debug port
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for debug port to be ready (up to 15 seconds)
        print("  ⏳ Waiting for Chrome debug port to open...", end="", flush=True)
        port_open = False
        for _ in range(30):
            time.sleep(0.5)
            try:
                s = socket.create_connection(("127.0.0.1", CHROME_DEBUG_PORT), timeout=1)
                s.close()
                port_open = True
                break
            except (ConnectionRefusedError, OSError):
                print(".", end="", flush=True)
        print()

        if not port_open:
            raise RuntimeError(
                f"Chrome debug port {CHROME_DEBUG_PORT} never opened. "
                "Make sure Chrome is fully closed before running."
            )

        print("  ✅ Chrome debug port is open, connecting Selenium...")
        options = Options()
        options.debugger_address = f"127.0.0.1:{CHROME_DEBUG_PORT}"
        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(0)
        print("  ✅ Connected to Chrome")

    def _kill_existing_chrome(self):
        """Kill any existing Chrome processes to free the debug port."""
        result = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        if pids:
            print(f"  🔧 Closing existing Chrome (PID {', '.join(pids)})...")
            for pid in pids:
                try:
                    subprocess.run(["kill", pid], capture_output=True)
                except Exception:
                    pass
            time.sleep(3)

    def _find_quick(self, *selectors):
        """Try multiple selectors with NO wait. Returns first match or None."""
        from selenium.common.exceptions import NoSuchElementException
        for selector in selectors:
            try:
                return self.driver.find_element(*selector)
            except NoSuchElementException:
                continue
        return None

    def _save_debug_screenshot(self, name: str):
        """Save a screenshot for debugging."""
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / f"{name}.png"
        try:
            self.driver.save_screenshot(str(path))
            print(f"  📸 Screenshot: {path.name}")
        except Exception:
            pass

    def _get_credentials(self) -> tuple[str, str]:
        """Load HUJI credentials from environment."""
        email = os.getenv("HUJI_EMAIL", "")
        password = os.getenv("HUJI_PASSWORD", "")
        if not email or not password:
            raise ValueError(
                "HUJI_EMAIL and HUJI_PASSWORD must be set in .env file. "
                "See .env.example for details."
            )
        return email, password

    def fetch_authenticated_html(self, article_url: str, publisher: str = "oup") -> str:
        """Navigate to article URL, handle Cloudflare + Shibboleth, return HTML."""
        self._ensure_driver()
        return self._fetch_oup(article_url)

    def _fetch_oup(self, article_url: str) -> str:
        """Fetch an OUP article: navigate to it, let user handle Cloudflare + login."""
        email, password = self._get_credentials()

        print(f"  🌐 Navigating to: {article_url}")
        self.driver.get(article_url)
        time.sleep(4)
        self._save_debug_screenshot("01_initial_page")
        print(f"  📍 URL: {self.driver.current_url}")

        # Check if we already have full-text access
        if self._has_full_text():
            print("  ✅ Full text already accessible!")
            return self.driver.page_source

        # Pause for user to handle Cloudflare + Shibboleth login manually
        print()
        print("  " + "─" * 60)
        print("  👤 ACTION REQUIRED in the Chrome window:")
        print("     1. If you see a Cloudflare challenge → solve it")
        print("     2. Click 'Sign in through your institution'")
        print("     3. Find and select 'Hebrew University of Jerusalem'")
        print(f"     4. Log in with: {email}")
        print("     5. Wait for the full article to load")
        print("  " + "─" * 60)
        input("\n  ▶ Press Enter here AFTER the full article is visible... ")
        print()

        self._save_debug_screenshot("02_after_manual_login")
        html = self.driver.page_source
        print(f"  📄 Got page HTML ({len(html):,} chars)")

        if not self._has_full_text():
            print("  ⚠ Full-text markers not detected — saving HTML anyway")

        self._authenticated = True
        return html

    def _has_full_text(self) -> bool:
        """Check if the current page has full-text article content."""
        from selenium.webdriver.common.by import By
        for sel in [".article-body", "#ContentTab", ".article-full-text",
                    "div.section[id*='s']"]:
            if self._find_quick((By.CSS_SELECTOR, sel)):
                return True
        return False

    def close(self):
        """Disconnect from Chrome (but don't close the browser)."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self._authenticated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Module-level singleton
_session: ShibbolethSession | None = None


def get_session(**kwargs) -> ShibbolethSession:
    """Get or create a shared ShibbolethSession."""
    global _session
    if _session is None:
        _session = ShibbolethSession()
    return _session


def close_session():
    """Close the shared ShibbolethSession."""
    global _session
    if _session is not None:
        _session.close()
        _session = None
