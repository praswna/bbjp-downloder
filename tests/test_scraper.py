"""Unit tests for the scraper/downloader — no network required.

We stub out HTTP by monkeypatching ``Scraper.get`` and ``Downloader._fetch``
with canned responses built from local HTML fixtures, so the parsing and
file-handling logic can be exercised deterministically.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from bbjp_downloader.config import Config
from bbjp_downloader.downloader import Downloader, sanitize_filename
from bbjp_downloader.scraper import Gallery, Scraper, slugify


class FakeResponse:
    def __init__(self, text="", content=b"", headers=None, status=200):
        self.text = text
        self._content = content
        self.headers = headers or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def close(self):
        pass


# --------------------------------------------------------------------------
# slugify
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Hitomi Tanaka", "hitomi-tanaka"),
    ("  Fancy   Name  ", "fancy-name"),
    ("Anri-Okita", "anri-okita"),
    ("Name!!!With???Punct", "namewithpunct"),
])
def test_slugify(name, expected):
    assert slugify(name) == expected


def test_slugify_preserves_unicode():
    # Japanese names should survive slugification (encoded later at request time)
    assert slugify("田中 舞") == "田中-舞"


# --------------------------------------------------------------------------
# sanitize_filename
# --------------------------------------------------------------------------

def test_sanitize_filename_strips_unsafe():
    assert sanitize_filename('a/b:c*?"<>|d') == "a_b_c_d"


def test_sanitize_filename_fallback():
    assert sanitize_filename("", "fallback") == "fallback"
    assert sanitize_filename("///", "fb") == "fb"


# --------------------------------------------------------------------------
# post-link extraction
# --------------------------------------------------------------------------

LISTING_HTML = """
<html><body>
  <article>
    <h2 class="entry-title"><a href="/gallery-one/">Gallery One</a></h2>
  </article>
  <article>
    <h2 class="entry-title"><a href="https://www.bigboobsjapan.com/gallery-two/">Two</a></h2>
  </article>
  <a href="/tag/some-name/">tag link (should be ignored)</a>
  <a href="/page/2/">pagination (ignored)</a>
  <a href="/photo.jpg">bare image (ignored)</a>
</body></html>
"""


def test_extract_post_links_filters_non_posts():
    scraper = Scraper(Config())
    links = scraper._extract_post_links(
        LISTING_HTML, "https://www.bigboobsjapan.com/tag/some-name/")
    assert links == [
        "https://www.bigboobsjapan.com/gallery-one/",
        "https://www.bigboobsjapan.com/gallery-two/",
    ]


# --------------------------------------------------------------------------
# image extraction
# --------------------------------------------------------------------------

GALLERY_HTML = """
<html><body>
  <h1 class="entry-title">Great Set</h1>
  <div class="entry-content">
    <a href="/wp-content/uploads/2020/01/img1.jpg">
      <img src="/wp-content/uploads/2020/01/img1-300x200.jpg"/>
    </a>
    <img src="/wp-content/uploads/2020/01/img2-1024x768.jpg"
         srcset="/wp-content/uploads/2020/01/img2-300x200.jpg 300w,
                 /wp-content/uploads/2020/01/img2-1024x768.jpg 1024w"/>
    <img data-src="/wp-content/uploads/2020/01/img3.png"/>
    <img src="/wp-content/uploads/logo.png"/>            <!-- blocked -->
    <img src="/wp-content/uploads/gravatar/avatar.jpg"/> <!-- blocked -->
  </div>
</body></html>
"""


def test_extract_images(monkeypatch):
    scraper = Scraper(Config(full_size=True))
    monkeypatch.setattr(
        scraper, "get",
        lambda url: FakeResponse(text=GALLERY_HTML))
    gallery = scraper.extract_images("https://www.bigboobsjapan.com/great-set/")
    assert gallery is not None
    assert gallery.title == "Great Set"
    base = "https://www.bigboobsjapan.com/wp-content/uploads/2020/01"
    # img1 from the <a href>; img2 upgraded to original; img3 from data-src.
    assert f"{base}/img1.jpg" in gallery.images
    assert f"{base}/img2.jpg" in gallery.images        # -1024x768 stripped
    assert f"{base}/img3.png" in gallery.images
    # Chrome images excluded.
    assert not any("logo" in u or "gravatar" in u for u in gallery.images)


def test_full_size_disabled_keeps_resized(monkeypatch):
    scraper = Scraper(Config(full_size=False))
    monkeypatch.setattr(
        scraper, "get",
        lambda url: FakeResponse(text=GALLERY_HTML))
    gallery = scraper.extract_images("https://www.bigboobsjapan.com/x/")
    base = "https://www.bigboobsjapan.com/wp-content/uploads/2020/01"
    # With full_size off, the srcset's largest (1024) is kept as-is.
    assert f"{base}/img2-1024x768.jpg" in gallery.images


def test_largest_from_srcset():
    srcset = "a-300.jpg 300w, b-1024.jpg 1024w, c-800.jpg 800w"
    assert Scraper._largest_from_srcset(srcset) == "b-1024.jpg"
    assert Scraper._largest_from_srcset(None) is None


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def test_download_gallery(tmp_path, monkeypatch):
    config = Config(output_dir=tmp_path, request_delay=0, max_workers=2)
    downloader = Downloader(config)

    payloads = {
        "https://x/img1.jpg": b"\xff\xd8\xff" + b"a" * 100,
        "https://x/img2.png": b"\x89PNG" + b"b" * 50,
    }

    def fake_fetch(url):
        return FakeResponse(content=payloads[url],
                            headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr(downloader, "_fetch", fake_fetch)

    gallery = Gallery(url="https://x/set/", title="My Set",
                      images=list(payloads))
    stats = downloader.download_gallery(gallery, tmp_path)

    assert stats.downloaded == 2
    assert stats.failed == 0
    folder = tmp_path / "My Set"
    files = sorted(p.name for p in folder.iterdir())
    assert len(files) == 2
    assert stats.bytes_written == sum(len(v) for v in payloads.values())


def test_download_skips_existing(tmp_path, monkeypatch):
    config = Config(output_dir=tmp_path, request_delay=0)
    downloader = Downloader(config)
    gallery = Gallery(url="https://x/s/", title="Set",
                      images=["https://x/a.jpg"])

    calls = {"n": 0}

    def fake_fetch(url):
        calls["n"] += 1
        return FakeResponse(content=b"data123",
                            headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr(downloader, "_fetch", fake_fetch)

    first = downloader.download_gallery(gallery, tmp_path)
    assert first.downloaded == 1
    second = downloader.download_gallery(gallery, tmp_path)
    assert second.skipped == 1
    assert calls["n"] == 1  # not fetched again on the second run


def test_download_missing_extension_uses_content_type(tmp_path, monkeypatch):
    config = Config(output_dir=tmp_path, request_delay=0)
    downloader = Downloader(config)
    gallery = Gallery(url="https://x/s/", title="Set",
                      images=["https://x/image-no-ext"])

    monkeypatch.setattr(
        downloader, "_fetch",
        lambda url: FakeResponse(content=b"pngdata",
                                 headers={"Content-Type": "image/png"}))
    stats = downloader.download_gallery(gallery, tmp_path)
    assert stats.downloaded == 1
    saved = list((tmp_path / "Set").iterdir())
    assert saved[0].suffix == ".png"
