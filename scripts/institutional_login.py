#!/usr/bin/env python3
"""Selenium-based institutional (Shibboleth/OpenAthens) login for accessing paywalled articles."""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


class ShibbolethSession:
    """Automates Shibboleth/OpenAthens institutional login via Selenium.

    Supports OUP and other publishers that use Shibboleth for institutional access.
    Caches the browser session so multiple articles can be fetched without re-login.
    """

    INSTITUTION_NAME = "Hebrew University of Jerusalem"

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver = None
        self._authenticated = False

    def _ensure_driver(self):
        """Initialize Selenium WebDriver if not already running."""
        if self.driver is not None:
            return

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        try:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
        except ImportError:
            service = Service()

        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.implicitly_wait(10)

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
        """Navigate to an article URL, authenticate via Shibboleth if needed, and return HTML.

        Args:
            article_url: Full URL of the article to fetch.
            publisher: Publisher key (used to select the correct login flow).

        Returns:
            The full HTML of the authenticated article page.
        """
        self._ensure_driver()

        if publisher == "oup":
            return self._fetch_oup(article_url)
        else:
            return self._fetch_generic_shibboleth(article_url)

    def _fetch_oup(self, article_url: str) -> str:
        """Fetch an OUP article via Shibboleth institutional login."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        email, password = self._get_credentials()
        self.driver.get(article_url)
        time.sleep(3)

        # Check if we already have access (cached session)
        if self._has_full_text():
            print("  ✅ Already authenticated (cached session)")
            return self.driver.page_source

        # Click "Sign in through your institution" or similar button
        login_clicked = False
        for selector in [
            (By.LINK_TEXT, "Sign in through your institution"),
            (By.PARTIAL_LINK_TEXT, "institution"),
            (By.PARTIAL_LINK_TEXT, "Institutional"),
            (By.CSS_SELECTOR, "a[href*='institutional']"),
            (By.CSS_SELECTOR, "a[href*='institution-login']"),
            (By.CSS_SELECTOR, "a[href*='Shibboleth']"),
            (By.CSS_SELECTOR, ".institutional-login"),
        ]:
            try:
                btn = self.driver.find_element(*selector)
                btn.click()
                login_clicked = True
                print("  🔑 Clicked institutional login link")
                time.sleep(3)
                break
            except NoSuchElementException:
                continue

        if not login_clicked:
            # Try direct institutional login URL
            login_url = article_url.split("?")[0] + "?login=true"
            self.driver.get(login_url)
            time.sleep(3)

        # Search for institution in the WAYF (Where Are You From) page
        self._select_institution()

        # Enter credentials on HUJI IdP page
        self._enter_credentials(email, password)

        # Wait for redirect back to the article
        try:
            WebDriverWait(self.driver, 30).until(
                lambda d: "academic.oup.com" in d.current_url
            )
            time.sleep(3)
        except TimeoutException:
            print(f"  ⚠ Redirect timeout. Current URL: {self.driver.current_url}")

        self._authenticated = True
        return self.driver.page_source

    def _select_institution(self):
        """Find and select HUJI in the institution selection (WAYF) page."""
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import NoSuchElementException

        time.sleep(2)

        # Try various institution search/selection patterns
        search_selectors = [
            (By.CSS_SELECTOR, "input[type='search']"),
            (By.CSS_SELECTOR, "input[type='text'][placeholder*='institution']"),
            (By.CSS_SELECTOR, "input[type='text'][placeholder*='search']"),
            (By.CSS_SELECTOR, "input#search"),
            (By.CSS_SELECTOR, "input.search"),
            (By.CSS_SELECTOR, "input[name='user_idp']"),
            (By.CSS_SELECTOR, "#idp-search"),
        ]

        for selector in search_selectors:
            try:
                search_box = self.driver.find_element(*selector)
                search_box.clear()
                search_box.send_keys("Hebrew University")
                time.sleep(2)

                # Click the matching result
                result_selectors = [
                    (By.XPATH, f"//*[contains(text(), '{self.INSTITUTION_NAME}')]"),
                    (By.XPATH, "//*[contains(text(), 'Hebrew University')]"),
                    (By.CSS_SELECTOR, ".result-item"),
                    (By.CSS_SELECTOR, ".institution-result"),
                    (By.CSS_SELECTOR, "li.suggestion"),
                ]
                for rs in result_selectors:
                    try:
                        result = self.driver.find_element(*rs)
                        result.click()
                        time.sleep(2)
                        return
                    except NoSuchElementException:
                        continue

                # If no clickable result, try submitting the form
                from selenium.webdriver.common.keys import Keys
                search_box.send_keys(Keys.RETURN)
                time.sleep(2)
                return
            except NoSuchElementException:
                continue

        # Fallback: look for a dropdown or direct link
        try:
            link = self.driver.find_element(
                By.XPATH, f"//a[contains(text(), 'Hebrew University')]"
            )
            link.click()
            time.sleep(2)
        except NoSuchElementException:
            print("  ⚠ Could not find institution selector. May already be on IdP page.")

    def _enter_credentials(self, email: str, password: str):
        """Enter credentials on the HUJI Shibboleth IdP login page."""
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import NoSuchElementException

        time.sleep(2)

        # Find username/email field
        username_selectors = [
            (By.CSS_SELECTOR, "input[type='email']"),
            (By.CSS_SELECTOR, "input[name='username']"),
            (By.CSS_SELECTOR, "input[name='j_username']"),
            (By.CSS_SELECTOR, "input[name='email']"),
            (By.CSS_SELECTOR, "input#username"),
            (By.CSS_SELECTOR, "input#email"),
            (By.CSS_SELECTOR, "input[type='text']"),
        ]

        for selector in username_selectors:
            try:
                field = self.driver.find_element(*selector)
                field.clear()
                field.send_keys(email)
                print("  📧 Entered email")
                break
            except NoSuchElementException:
                continue

        # Find password field
        try:
            pwd_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            pwd_field.clear()
            pwd_field.send_keys(password)
            print("  🔒 Entered password")
        except NoSuchElementException:
            print("  ⚠ No password field found")
            return

        # Submit the form
        submit_selectors = [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button.login"),
            (By.CSS_SELECTOR, "#login-button"),
        ]
        for selector in submit_selectors:
            try:
                btn = self.driver.find_element(*selector)
                btn.click()
                print("  ✅ Submitted login form")
                time.sleep(5)
                return
            except NoSuchElementException:
                continue

        # Fallback: press Enter on password field
        from selenium.webdriver.common.keys import Keys
        pwd_field.send_keys(Keys.RETURN)
        time.sleep(5)

    def _has_full_text(self) -> bool:
        """Check if the current page has full-text article content."""
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import NoSuchElementException

        indicators = [
            (By.CSS_SELECTOR, ".article-body"),
            (By.CSS_SELECTOR, "#ContentTab"),
            (By.CSS_SELECTOR, ".article-full-text"),
            (By.CSS_SELECTOR, "div.section[id*='s']"),
        ]
        for selector in indicators:
            try:
                self.driver.find_element(*selector)
                return True
            except NoSuchElementException:
                continue
        return False

    def _fetch_generic_shibboleth(self, article_url: str) -> str:
        """Generic Shibboleth login flow for non-OUP publishers."""
        # Same general flow — navigate, find institution, enter credentials
        return self._fetch_oup(article_url)

    def close(self):
        """Close the Selenium browser session."""
        if self.driver:
            self.driver.quit()
            self.driver = None
            self._authenticated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Module-level singleton for session reuse across multiple fetches
_session: ShibbolethSession | None = None


def get_session(headless: bool = True) -> ShibbolethSession:
    """Get or create a shared ShibbolethSession."""
    global _session
    if _session is None:
        _session = ShibbolethSession(headless=headless)
    return _session


def close_session():
    """Close the shared ShibbolethSession."""
    global _session
    if _session is not None:
        _session.close()
        _session = None
