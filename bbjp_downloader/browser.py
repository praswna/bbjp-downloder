"""Selenium-backed scraping, for sites that block plain HTTP requests.

``BrowserScraper`` subclasses :class:`~bbjp_downloader.scraper.Scraper` and
overrides just one method — :meth:`get` — so every piece of proven parsing and
discovery logic (tag/category routing, pagination, homepage-fallback detection,
listing-stub and image extraction) runs exactly as before, but the page HTML
comes from a real Chrome browser instead of ``requests``. That bypasses the
JavaScript / anti-bot challenges that return blocked or empty pages to a bare
HTTP client.

This mirrors the approach of the reference implementation (which drove Chrome
with Selenium). Downloads then reuse the browser's cookies via
:func:`download_session`, so hot-link / referer checks pass too.

Importing this module requires ``selenium``; callers should handle ImportError
and fall back to the HTTP scraper (or tell the user to install it).
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from .config import Config
from .scraper import Scraper

logger = logging.getLogger(__name__)


def selenium_available() -> bool:
    """True if the ``selenium`` package can be imported."""
    try:
        import selenium  # noqa: F401
        return True
    except Exception:
        return False


class _PageResponse:
    """Minimal requests.Response stand-in wrapping ``driver.page_source``."""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        pass


class BrowserScraper(Scraper):
    """A :class:`Scraper` whose ``get`` loads pages in a real Chrome browser."""

    def __init__(self, config: Config, cancel_event=None):
        super().__init__(config, cancel_event=cancel_event)
        self._driver = None
        self._driver_lock = threading.Lock()

    # ---- driver lifecycle -------------------------------------------------

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        from selenium import webdriver  # imported lazily so import is optional

        opts = webdriver.ChromeOptions()
        # Reduce automation fingerprinting so anti-bot checks are less likely
        # to flag the session.
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,1400")
        opts.add_argument(f"--user-agent={self.config.user_agent}")
        if self.config.browser_headless:
            opts.add_argument("--headless=new")

        logger.info("launching Chrome (browser mode)…")
        # Selenium 4's Selenium Manager fetches a matching chromedriver, so no
        # manual driver path is needed as long as Chrome is installed.
        self._driver = webdriver.Chrome(options=opts)
        self._driver.set_page_load_timeout(max(self.config.timeout * 2, 60))
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',"
                           "{get:()=>undefined})"})
        except Exception:
            pass
        return self._driver

    def close(self) -> None:
        with self._driver_lock:
            if self._driver is not None:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None

    # ---- overridden transport --------------------------------------------

    def get(self, url: str):  # type: ignore[override]
        if self._cancelled():
            return None
        with self._driver_lock:
            if self._cancelled():
                return None
            self._throttle()
            try:
                driver = self._ensure_driver()
                driver.get(url)
            except Exception as exc:
                logger.warning("browser failed to load %s: %s", url, exc)
                return None
            # Let JavaScript / any anti-bot challenge settle.
            self._wait_ready(driver)
            return _PageResponse(driver.page_source)

    def _wait_ready(self, driver) -> None:
        deadline = time.monotonic() + max(self.config.timeout, 15)
        while time.monotonic() < deadline:
            try:
                if driver.execute_script("return document.readyState") == "complete":
                    break
            except Exception:
                break
            time.sleep(0.2)
        if self.config.browser_settle > 0:
            time.sleep(self.config.browser_settle)

    # ---- cookies for downloading -----------------------------------------

    def cookies(self) -> dict:
        with self._driver_lock:
            if self._driver is None:
                return {}
            try:
                return {c["name"]: c["value"]
                        for c in self._driver.get_cookies()}
            except Exception:
                return {}


def download_session(config: Config, cookies: dict | None = None) -> requests.Session:
    """A requests session seeded with the browser's cookies + headers, so image
    downloads pass the same referer / hot-link / anti-bot checks."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": config.user_agent,
        "Referer": config.base_url,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    })
    for name, value in (cookies or {}).items():
        try:
            session.cookies.set(name, value)
        except Exception:
            pass
    return session
