"""Tests for BrowserScraper — mocking Selenium's Chrome driver so no real
browser is launched. The point of these tests is to verify that overriding
just ``get()`` correctly reuses every inherited discovery/extraction method
from :class:`Scraper` unchanged.
"""

from __future__ import annotations

import threading

import pytest

selenium = pytest.importorskip("selenium")

from bbjp_downloader.browser import BrowserScraper, download_session
from bbjp_downloader.config import Config


class FakeDriver:
    """Stands in for selenium.webdriver.Chrome."""

    def __init__(self, pages: dict[str, str], cookies=None):
        self.pages = pages
        self._cookies = cookies or [{"name": "sess", "value": "abc123"}]
        self.page_source = ""
        self.visited: list[str] = []
        self.quit_called = False
        self.timeout_set = None

    def set_page_load_timeout(self, seconds):
        self.timeout_set = seconds

    def execute_cdp_cmd(self, *a, **k):
        pass

    def get(self, url):
        self.visited.append(url)
        self.page_source = self.pages.get(url, "<html></html>")

    def execute_script(self, script):
        return "complete"

    def get_cookies(self):
        return self._cookies

    def quit(self):
        self.quit_called = True


@pytest.fixture
def patched_chrome(monkeypatch):
    """Monkeypatch selenium.webdriver.Chrome to return a FakeDriver."""
    from selenium import webdriver as sel_webdriver

    holder: dict = {}

    def fake_chrome(options=None):
        driver = FakeDriver(holder.get("pages", {}), holder.get("cookies"))
        holder["driver"] = driver
        return driver

    monkeypatch.setattr(sel_webdriver, "Chrome", fake_chrome)
    return holder


TAG_LISTING = """
<html><body><div id="content">
  <article><div class="entry-featured-img-wrap">
    <a class="entry-featured-img-link"
       href="https://www.bigboobsjapan.com/2020/01/15/set-a/"></a></div></article>
</div></body></html>
"""

POST_HTML = """
<html><body><h1 class="entry-title">A Set</h1><div class="entry-content">
  <div class="gallery-item col-3"><a href="/2020/01/15/set-a/p1/">
    <img srcset="/wp-content/uploads/2020/01/p1-768x1024.jpg 768w,
                 /wp-content/uploads/2020/01/p1-1600x2133.jpg 1600w"></a></div>
</div></body></html>
"""


def test_browser_get_returns_page_source(patched_chrome):
    patched_chrome["pages"] = {
        "https://www.bigboobsjapan.com/tag/X/": TAG_LISTING,
    }
    scraper = BrowserScraper(Config(obey_robots=False, request_delay=0,
                                    browser_settle=0))
    resp = scraper.get("https://www.bigboobsjapan.com/tag/X/")
    assert resp is not None
    assert resp.status_code == 200
    assert "entry-featured-img-link" in resp.text
    assert patched_chrome["driver"].visited == ["https://www.bigboobsjapan.com/tag/X/"]
    scraper.close()
    assert patched_chrome["driver"].quit_called


def test_browser_reuses_inherited_discovery_and_extraction(patched_chrome, monkeypatch):
    """The whole point of BrowserScraper: only transport changes, all the
    proven parsing logic (listing/pagination/gallery extraction) still runs."""
    base = "https://www.bigboobsjapan.com"
    patched_chrome["pages"] = {
        f"{base}/tag/X/": TAG_LISTING,
        f"{base}/2020/01/15/set-a/": POST_HTML,
        base: "<html><body></body></html>",  # homepage, for fallback check
    }
    scraper = BrowserScraper(Config(obey_robots=False, request_delay=0,
                                    browser_settle=0))
    # Skip the search/discovery/guess machinery — go straight to a known URL.
    monkeypatch.setattr(scraper, "_listing_urls",
                        lambda name: [(f"{base}/tag/X/", True, False)])

    galleries = scraper.find_galleries("whoever")
    assert len(galleries) == 1
    g = galleries[0]
    assert g.title == "A Set"
    assert g.images == [f"{base}/wp-content/uploads/2020/01/p1-1600x2133.jpg"]
    scraper.close()


def test_browser_cancel_avoids_launch(patched_chrome):
    cancel = threading.Event()
    cancel.set()
    scraper = BrowserScraper(Config(obey_robots=False), cancel_event=cancel)
    assert scraper.get("https://www.bigboobsjapan.com/tag/X/") is None
    assert "driver" not in patched_chrome  # never launched Chrome


def test_browser_cookies(patched_chrome):
    patched_chrome["cookies"] = [{"name": "cf_clearance", "value": "zzz"}]
    patched_chrome["pages"] = {"https://x/": "<html></html>"}
    scraper = BrowserScraper(Config(obey_robots=False, request_delay=0,
                                    browser_settle=0))
    assert scraper.cookies() == {}          # no driver launched yet
    scraper.get("https://x/")
    assert scraper.cookies() == {"cf_clearance": "zzz"}
    scraper.close()
    assert scraper.cookies() == {}           # closed -> no driver


def test_download_session_seeds_cookies_and_headers():
    cfg = Config(base_url="https://www.bigboobsjapan.com")
    session = download_session(cfg, {"a": "1", "b": "2"})
    assert session.headers["Referer"] == cfg.base_url
    assert session.headers["User-Agent"] == cfg.user_agent
    jar = {c.name: c.value for c in session.cookies}
    assert jar == {"a": "1", "b": "2"}


def test_download_session_handles_no_cookies():
    session = download_session(Config())
    assert list(session.cookies) == []
