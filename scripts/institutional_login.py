#!/usr/bin/env python3
"""Selenium-based institutional (Shibboleth/OpenAthens) login for accessing paywalled articles.

Strategy: Connect to the user's real Chrome browser via remote debugging port.
This bypasses Cloudflare bot detection since it IS a real browser session.

Features:
  - Cookie persistence: saves/restores auth cookies to skip re-login
  - Automated Shibboleth login: handles institution selection + credential entry
  - Session reuse: keeps Chrome alive across batch runs
  - Manual CAPTCHA fallback: only pauses when Cloudflare challenge is detected

Usage:
  1. Close Chrome completely (Cmd+Q) on first run
  2. The script auto-launches Chrome with remote debugging enabled
  3. If Cloudflare CAPTCHA appears, solve it manually in the browser
  4. The script handles the rest (Shibboleth login, HTML extraction)
  5. Subsequent runs reuse the existing Chrome session
"""

import json
import os
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DEBUG_DIR = ROOT_DIR / "data" / "_debug"
COOKIE_DIR = ROOT_DIR / "data" / "_cookies"
CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_DEBUG_PORT = 9222
CHROME_USER_DATA_DIR = str(Path.home() / "Library/Application Support/Google/Chrome")
CHROME_PROFILE_DIR = "Profile 3"  # HUJI account (alon.nissan1@mail.huji.ac.il)

COOKIE_TTL_SECONDS = 4 * 60 * 60  # 4 hours

# Full-text selectors by publisher
_FULL_TEXT_SELECTORS = {
    "oup": [
        ".article-body", "#ContentTab", ".article-full-text",
        "div.section[id*='s']",
    ],
    "wiley": [
        ".article-section__content", ".article__body", "#article-content",
    ],
    "generic": [
        "article", ".full-text", "#full-text",
    ],
}

# Patterns that indicate a Cloudflare challenge page
_CLOUDFLARE_MARKERS = ["cf-challenge", "challenge-platform", "cf-turnstile-wrapper"]

# Link text patterns for "Sign in through your institution"
_INSTITUTION_LINK_PATTERNS = [
    "sign in through your institution",
    "institutional login",
    "log in via openathens",
    "access through your institution",
    "institutional access",
    "log in through your institution",
]


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

    # ── Driver lifecycle ──────────────────────────────────────────────

    def _is_driver_alive(self) -> bool:
        """Check whether the existing Selenium driver is still responsive."""
        if self.driver is None:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def _ensure_driver(self):
        """Launch Chrome with remote debugging and connect Selenium to it."""
        if self._is_driver_alive():
            return

        # Driver is stale or None — reset it
        self.driver = None

        import socket
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        # Only kill Chrome if we can't connect to an existing debug port
        if not self._debug_port_open():
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
                    "--no-startup-window",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

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
        else:
            print(f"  🔗 Reusing existing Chrome on port {CHROME_DEBUG_PORT}")

        print("  ✅ Chrome debug port is open, connecting Selenium...")
        options = Options()
        options.debugger_address = f"127.0.0.1:{CHROME_DEBUG_PORT}"
        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(0)
        print("  ✅ Connected to Chrome")

    def _debug_port_open(self) -> bool:
        """Check if the Chrome debug port is already listening."""
        import socket
        try:
            s = socket.create_connection(("127.0.0.1", CHROME_DEBUG_PORT), timeout=1)
            s.close()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    def _kill_existing_chrome(self):
        """Kill any existing Chrome processes to free the debug port."""
        result = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            capture_output=True, text=True,
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

    # ── Element helpers ───────────────────────────────────────────────

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

    # ── Cookie persistence ────────────────────────────────────────────

    def save_cookies(self, publisher: str) -> None:
        """Serialize browser cookies to disk for later reuse."""
        if self.driver is None:
            return
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        cookie_path = COOKIE_DIR / f"{publisher}_cookies.json"
        try:
            cookies = self.driver.get_cookies()
            payload = {
                "timestamp": time.time(),
                "cookies": cookies,
            }
            cookie_path.write_text(json.dumps(payload, indent=2, default=str))
            print(f"  🍪 Saved {len(cookies)} cookies → {cookie_path.name}")
        except Exception as exc:
            print(f"  ⚠ Failed to save cookies: {exc}")

    def load_cookies(self, publisher: str, ttl: int = COOKIE_TTL_SECONDS) -> list[dict] | None:
        """Load cookies from disk if they exist and haven't expired.

        Expiry is based on the file's modification time, not individual cookie expiry.
        Returns the cookie list or None if stale / missing.
        """
        cookie_path = COOKIE_DIR / f"{publisher}_cookies.json"
        if not cookie_path.exists():
            return None

        age = time.time() - cookie_path.stat().st_mtime
        if age > ttl:
            print(f"  🍪 Cookies for {publisher} expired ({age / 3600:.1f}h old, TTL={ttl / 3600:.1f}h)")
            return None

        try:
            payload = json.loads(cookie_path.read_text())
            cookies = payload.get("cookies", [])
            print(f"  🍪 Loaded {len(cookies)} cached cookies for {publisher} ({age / 60:.0f}m old)")
            return cookies
        except Exception as exc:
            print(f"  ⚠ Failed to load cookies: {exc}")
            return None

    # ── Full-text detection ───────────────────────────────────────────

    def _has_full_text(self, publisher: str = "oup") -> bool:
        """Check if the current page has full-text article content."""
        from selenium.webdriver.common.by import By

        selectors: list[str] = []
        selectors.extend(_FULL_TEXT_SELECTORS.get(publisher, []))
        if publisher != "generic":
            selectors.extend(_FULL_TEXT_SELECTORS["generic"])

        for sel in selectors:
            if self._find_quick((By.CSS_SELECTOR, sel)):
                return True
        return False

    # ── Cloudflare handling ───────────────────────────────────────────

    def _has_cloudflare_challenge(self) -> bool:
        """Return True if the current page contains a Cloudflare challenge."""
        try:
            src = self.driver.page_source.lower()
            return any(marker in src for marker in _CLOUDFLARE_MARKERS)
        except Exception:
            return False

    def _wait_for_cloudflare(self, timeout: int = 120) -> None:
        """If a Cloudflare challenge is present, ask the user to solve it and wait."""
        if not self._has_cloudflare_challenge():
            return

        print()
        print("  " + "─" * 60)
        print("  🛡️  Cloudflare challenge detected!")
        print("  👤 Please solve the CAPTCHA in the Chrome window.")
        print("  " + "─" * 60)
        self._save_debug_screenshot("cloudflare_challenge")

        elapsed = 0
        while elapsed < timeout:
            time.sleep(2)
            elapsed += 2
            if not self._has_cloudflare_challenge():
                print("  ✅ Cloudflare challenge cleared!")
                time.sleep(1)
                return
            if elapsed % 10 == 0:
                print(f"  ⏳ Still waiting for CAPTCHA solve... ({elapsed}s)")

        raise TimeoutError(f"Cloudflare challenge not solved within {timeout}s")

    # ── Automated Shibboleth flow ─────────────────────────────────────

    def _click_institutional_login(self) -> bool:
        """Find and click the 'Sign in through your institution' link/button."""
        from selenium.webdriver.common.by import By

        # Try partial link text matching
        for pattern in _INSTITUTION_LINK_PATTERNS:
            try:
                links = self.driver.find_elements(By.XPATH,
                    f"//a[contains(translate(text(),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{pattern}')]"
                )
                if links:
                    links[0].click()
                    print(f"  🔗 Clicked institutional login link: '{pattern}'")
                    return True
            except Exception:
                continue

        # Also try buttons
        for pattern in _INSTITUTION_LINK_PATTERNS:
            try:
                buttons = self.driver.find_elements(By.XPATH,
                    f"//button[contains(translate(text(),"
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                    f"'{pattern}')]"
                )
                if buttons:
                    buttons[0].click()
                    print(f"  🔗 Clicked institutional login button: '{pattern}'")
                    return True
            except Exception:
                continue

        print("  ⚠ Could not find institutional login link")
        return False

    def _select_institution(self, timeout: int = 15) -> bool:
        """On the institution search page, type the name and select the result."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(self.driver, timeout)

        # Look for a search/filter input field
        search_selectors = [
            "input[type='search']",
            "input[type='text'][placeholder*='nstitution']",
            "input[type='text'][placeholder*='search']",
            "input[name='institution']",
            "input#institutionSearch",
            "input.institution-search",
            "#idpSelectInput",
            "input[id*='search']",
        ]

        search_input = None
        for sel in search_selectors:
            try:
                search_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                break
            except Exception:
                continue

        if search_input is None:
            print("  ⚠ Could not find institution search input")
            self._save_debug_screenshot("no_institution_search")
            return False

        search_input.clear()
        search_input.send_keys("Hebrew University")
        print("  ⌨️  Typed 'Hebrew University' in institution search")
        time.sleep(2)
        self._save_debug_screenshot("institution_search_results")

        # Click the matching result
        result_xpaths = [
            f"//*[contains(text(), '{self.INSTITUTION_NAME}')]",
            "//li[contains(@class, 'result')]",
            "//div[contains(@class, 'result')]",
            "//option[contains(text(), 'Hebrew')]",
        ]

        for xpath in result_xpaths:
            try:
                results = self.driver.find_elements(By.XPATH, xpath)
                for result in results:
                    if "hebrew" in result.text.lower() and "university" in result.text.lower():
                        result.click()
                        print(f"  🏛️  Selected institution: {result.text.strip()}")
                        return True
                # If no exact match, click first result for the first xpath
                if results and xpath == result_xpaths[0]:
                    results[0].click()
                    print(f"  🏛️  Selected institution: {results[0].text.strip()}")
                    return True
            except Exception:
                continue

        # Try submitting a form if present (some IdP pages use a submit button)
        try:
            submit = self._find_quick(
                (By.CSS_SELECTOR, "input[type='submit']"),
                (By.CSS_SELECTOR, "button[type='submit']"),
            )
            if submit:
                submit.click()
                print("  🏛️  Submitted institution selection form")
                return True
        except Exception:
            pass

        print("  ⚠ Could not select institution from search results")
        self._save_debug_screenshot("institution_select_failed")
        return False

    def _fill_credentials(self, timeout: int = 15) -> bool:
        """Find email/username + password fields, fill them, and submit."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        email, password = self._get_credentials()
        wait = WebDriverWait(self.driver, timeout)

        # Wait for a password field to appear (signals the login form is loaded)
        try:
            pass_field = wait.until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "input[type='password']",
            )))
        except Exception:
            print("  ⚠ Login form not found (no password field)")
            self._save_debug_screenshot("no_login_form")
            return False

        # Find the username/email field
        user_selectors = [
            "input[type='email']",
            "input[name='username']",
            "input[name='email']",
            "input[name='j_username']",
            "input[name='user']",
            "input[name='login']",
            "input[type='text']",
        ]
        user_field = None
        for sel in user_selectors:
            user_field = self._find_quick((By.CSS_SELECTOR, sel))
            if user_field:
                break

        if user_field is None:
            print("  ⚠ Could not find username/email field")
            self._save_debug_screenshot("no_user_field")
            return False

        user_field.clear()
        user_field.send_keys(email)
        pass_field.clear()
        pass_field.send_keys(password)
        print(f"  🔑 Filled credentials for {email}")
        self._save_debug_screenshot("credentials_filled")

        # Submit the form
        try:
            submit = self._find_quick(
                (By.CSS_SELECTOR, "button[type='submit']"),
                (By.CSS_SELECTOR, "input[type='submit']"),
                (By.CSS_SELECTOR, "button.login-button"),
                (By.CSS_SELECTOR, "button#login"),
            )
            if submit:
                submit.click()
            else:
                from selenium.webdriver.common.keys import Keys
                pass_field.send_keys(Keys.RETURN)
            print("  📨 Submitted login form")
            return True
        except Exception as exc:
            print(f"  ⚠ Failed to submit login form: {exc}")
            return False

    def _wait_for_full_text(self, publisher: str, timeout: int = 60) -> bool:
        """Poll until the full-text article content appears on the page."""
        elapsed = 0
        while elapsed < timeout:
            if self._has_full_text(publisher):
                return True
            time.sleep(2)
            elapsed += 2
            if elapsed % 10 == 0:
                print(f"  ⏳ Waiting for full text... ({elapsed}s)")
        return False

    def _fetch_with_shibboleth(self, article_url: str, publisher: str = "oup") -> str:
        """Navigate to an article URL and automate the full Shibboleth login flow.

        Only pauses for manual interaction if a Cloudflare CAPTCHA is detected.
        """
        from selenium.webdriver.support.ui import WebDriverWait

        print(f"  🌐 Navigating to: {article_url}")
        self.driver.get(article_url)

        # Wait for initial page load
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
        time.sleep(2)

        self._save_debug_screenshot("01_initial_page")
        print(f"  📍 URL: {self.driver.current_url}")

        # Already have full text (session still active)?
        if self._has_full_text(publisher):
            print("  ✅ Full text already accessible!")
            self.save_cookies(publisher)
            return self.driver.page_source

        # Handle Cloudflare challenge (manual CAPTCHA)
        self._wait_for_cloudflare()
        self._save_debug_screenshot("02_post_cloudflare")

        # Re-check after Cloudflare
        if self._has_full_text(publisher):
            print("  ✅ Full text accessible after Cloudflare!")
            self.save_cookies(publisher)
            return self.driver.page_source

        # Click "Sign in through your institution"
        if self._click_institutional_login():
            time.sleep(3)
            self._save_debug_screenshot("03_after_institution_link")

            # Select institution from search page
            if self._select_institution():
                time.sleep(3)
                self._save_debug_screenshot("04_after_institution_select")

                # Fill credentials on IdP login page
                if self._fill_credentials():
                    time.sleep(3)
                    self._save_debug_screenshot("05_after_credential_submit")

                    # Handle potential Cloudflare on IdP redirect
                    self._wait_for_cloudflare()

        # Wait for redirect back to article with full text
        print("  ⏳ Waiting for article full text to appear...")
        if self._wait_for_full_text(publisher, timeout=60):
            print("  ✅ Full text loaded!")
        else:
            print("  ⚠ Full-text markers not detected after login flow")

        self._save_debug_screenshot("06_final_page")
        html = self.driver.page_source
        print(f"  📄 Got page HTML ({len(html):,} chars)")

        self._authenticated = True
        self.save_cookies(publisher)
        return html

    # ── Public API ────────────────────────────────────────────────────

    def fetch_authenticated_html(self, article_url: str, publisher: str = "oup") -> str:
        """Navigate to article URL, handle Cloudflare + Shibboleth, return HTML.

        If already authenticated in this session, skip the full login flow and
        just navigate to the URL.
        """
        self._ensure_driver()

        # Fast path: already authenticated in this session
        if self._authenticated:
            print(f"  🔄 Session active — navigating directly to: {article_url}")
            self.driver.get(article_url)
            try:
                from selenium.webdriver.support.ui import WebDriverWait
                WebDriverWait(self.driver, 15).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
            time.sleep(2)

            if self._has_full_text(publisher):
                print("  ✅ Full text accessible (session reuse)")
                return self.driver.page_source
            else:
                print("  ⚠ Session may have expired — running full login flow")
                self._authenticated = False

        return self._fetch_with_shibboleth(article_url, publisher)

    def fetch_batch(self, urls: list[str], publisher: str = "oup") -> list[str]:
        """Fetch multiple article URLs, only logging in once.

        Returns a list of HTML strings (one per URL). On failure for a URL,
        an empty string is placed at that index.
        """
        results: list[str] = []
        total = len(urls)

        for i, url in enumerate(urls, 1):
            print()
            print(f"  {'━' * 60}")
            print(f"  📚 Batch [{i}/{total}]: {url}")
            print(f"  {'━' * 60}")
            try:
                html = self.fetch_authenticated_html(url, publisher)
                results.append(html)
            except Exception as exc:
                print(f"  ❌ Failed to fetch: {exc}")
                self._save_debug_screenshot(f"batch_error_{i}")
                results.append("")

        print(f"\n  📊 Batch complete: {sum(1 for h in results if h)}/{total} succeeded")
        return results

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


# ── Module-level singleton ────────────────────────────────────────────

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


def get_saved_cookies_for_requests(publisher: str) -> dict | None:
    """Load cached cookies and return them as a dict for ``requests.Session.cookies``.

    Returns None if no valid (non-expired) cookies are available on disk.
    """
    session = ShibbolethSession()
    cookies = session.load_cookies(publisher)
    if cookies is None:
        return None
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
