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
from bbjp_downloader.scraper import (
    Gallery,
    Scraper,
    is_url,
    person_label,
    slugify,
)

# A real-world category URL: romaji name + URL-encoded Japanese name.
MIURA_URL = (
    "https://www.bigboobsjapan.com/category/"
    "miura-sakura-%e6%b0%b4%e5%8d%9c%e3%81%95%e3%81%8f%e3%82%89/"
)


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
# URL / name resolution
# --------------------------------------------------------------------------

def test_is_url():
    assert is_url(MIURA_URL)
    assert is_url("http://example.com")
    assert not is_url("Miura Sakura")
    assert not is_url("")
    assert not is_url("just/a/path")


def test_person_label_from_url_decodes_slug():
    assert person_label(MIURA_URL) == "miura-sakura-水卜さくら"


def test_person_label_plain_name():
    assert person_label("Miura Sakura") == "Miura Sakura"


def test_listing_urls_passthrough_for_url():
    scraper = Scraper(Config())
    assert scraper._listing_urls(MIURA_URL) == [(MIURA_URL, True)]


def test_listing_urls_tries_category_tag_then_search(monkeypatch):
    scraper = Scraper(Config())
    monkeypatch.setattr(scraper, "_discover_taxonomy_urls", lambda name: [])
    urls = scraper._listing_urls("Miura Sakura")
    plain = [u for u, _ in urls]
    assert "https://www.bigboobsjapan.com/category/miura-sakura/" in plain
    assert "https://www.bigboobsjapan.com/tag/miura-sakura/" in plain
    # Search is present and flagged non-authoritative.
    assert any(u.endswith("/?s=Miura+Sakura") and not auth for u, auth in urls)
    # Category is tried before the tag guess.
    assert plain.index("https://www.bigboobsjapan.com/category/miura-sakura/") \
        < plain.index("https://www.bigboobsjapan.com/tag/miura-sakura/")


SEARCH_HTML = """
<html><body>
  <article>
    <h2 class="entry-title"><a href="/some-post/">A post</a></h2>
    <a href="/category/miura-sakura-%e6%b0%b4%e5%8d%9c%e3%81%95%e3%81%8f%e3%82%89/">
      Miura Sakura</a>
  </article>
  <a href="/category/other-person/">Someone else</a>
  <a href="/tag/miura-sakura-photos/">tag link</a>
</body></html>
"""


def test_discover_taxonomy_urls_matches_all_tokens(monkeypatch):
    scraper = Scraper(Config())
    monkeypatch.setattr(
        scraper, "get", lambda url: FakeResponse(text=SEARCH_HTML))
    found = scraper._discover_taxonomy_urls("Miura Sakura")
    assert MIURA_URL in found                       # exact category discovered
    assert any("miura-sakura-photos" in u for u in found)  # matching tag too
    assert all("other-person" not in u for u in found)     # unrelated excluded


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

# Mirrors the real theme: WordPress "gallery-item" blocks whose <a> points at
# an attachment *page* (not a file) and whose <img> carries a srcset.
GALLERY_HTML = """
<html><body>
  <h1 class="entry-title">Great Set</h1>
  <div class="entry-content">
    <div class="gallery">
      <div class="gallery-item col-3">
        <a href="https://www.bigboobsjapan.com/great-set/img1/">
          <img src="/wp-content/uploads/2020/01/img1-150x150.jpg"
               srcset="/wp-content/uploads/2020/01/img1-768x1024.jpg 768w,
                       /wp-content/uploads/2020/01/img1-1024x1365.jpg 1024w"/>
        </a>
      </div>
      <div class="gallery-item col-3">
        <a href="https://www.bigboobsjapan.com/great-set/img2/">
          <img src="/wp-content/uploads/2020/01/img2-150x150.jpg"
               srcset="/wp-content/uploads/2020/01/img2-1024x1365.jpg 1024w,
                       /wp-content/uploads/2020/01/img2-768x1024.jpg 768w"/>
        </a>
      </div>
    </div>
    <img src="/wp-content/uploads/logo.png"/>   <!-- chrome, outside gallery -->
  </div>
</body></html>
"""


def test_extract_images_uses_largest_gallery_srcset(monkeypatch):
    scraper = Scraper(Config())
    monkeypatch.setattr(
        scraper, "get", lambda url: FakeResponse(text=GALLERY_HTML))
    gallery = scraper.extract_images("https://www.bigboobsjapan.com/great-set/")
    assert gallery is not None
    assert gallery.title == "Great Set"
    base = "https://www.bigboobsjapan.com/wp-content/uploads/2020/01"
    # The largest srcset candidate is taken for each gallery image, verbatim
    # (no suffix stripping in the scraper).
    assert f"{base}/img1-1024x1365.jpg" in gallery.images
    assert f"{base}/img2-1024x1365.jpg" in gallery.images
    # Thumbnails, the logo, and attachment-page anchors are excluded.
    assert all("150x150" not in u for u in gallery.images)
    assert not any("logo" in u for u in gallery.images)
    assert not any(u.rstrip("/").endswith("img1") for u in gallery.images)


def test_extract_images_falls_back_to_content_imgs(monkeypatch):
    # A page with no gallery-item blocks: every <img> in the content is used.
    html = """
    <html><body><div class="entry-content">
      <img src="/wp-content/uploads/a-1024x768.jpg"
           srcset="/wp-content/uploads/a-1024x768.jpg 1024w"/>
      <img data-src="/wp-content/uploads/b.png"/>
    </div></body></html>
    """
    scraper = Scraper(Config())
    monkeypatch.setattr(scraper, "get", lambda url: FakeResponse(text=html))
    gallery = scraper.extract_images("https://www.bigboobsjapan.com/x/")
    urls = gallery.images
    assert "https://www.bigboobsjapan.com/wp-content/uploads/a-1024x768.jpg" in urls
    assert "https://www.bigboobsjapan.com/wp-content/uploads/b.png" in urls


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


def test_full_size_tries_original_first(tmp_path, monkeypatch):
    config = Config(output_dir=tmp_path, request_delay=0, full_size=True)
    downloader = Downloader(config)
    tried: list[str] = []

    def fake_fetch(url):
        tried.append(url)
        return FakeResponse(content=b"orig",
                            headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr(downloader, "_fetch", fake_fetch)
    gallery = Gallery(url="https://x/s/", title="S",
                      images=["https://x/photo-1024x768.jpg"])
    stats = downloader.download_gallery(gallery, tmp_path)
    assert stats.downloaded == 1
    # The un-resized original is attempted before the page's URL.
    assert tried[0] == "https://x/photo.jpg"


def test_full_size_falls_back_to_resized(tmp_path, monkeypatch):
    config = Config(output_dir=tmp_path, request_delay=0, full_size=True)
    downloader = Downloader(config)

    def fake_fetch(url):
        if url == "https://x/photo.jpg":       # original missing (404 -> None)
            return None
        return FakeResponse(content=b"resized",
                            headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr(downloader, "_fetch", fake_fetch)
    gallery = Gallery(url="https://x/s/", title="S",
                      images=["https://x/photo-1024x768.jpg"])
    stats = downloader.download_gallery(gallery, tmp_path)
    assert stats.downloaded == 1
    saved = list((tmp_path / "S").iterdir())
    assert saved and saved[0].read_bytes() == b"resized"


# --------------------------------------------------------------------------
# theme-specific selectors (from the reference implementation)
# --------------------------------------------------------------------------

FEATURED_LISTING_HTML = """
<html><body>
  <div id="content">
    <article>
      <div class="entry-featured-img-wrap">
        <a class="entry-featured-img-link"
           href="https://www.bigboobsjapan.com/2020/01/15/set-a/"></a>
      </div>
    </article>
    <article>
      <div class="entry-featured-img-wrap">
        <a class="entry-featured-img-link" href="/2021/06/02/set-b/"></a>
      </div>
    </article>
    <nav><div>
      <a class="page-numbers" href="/tag/x/">1</a>
      <a class="next page-numbers" href="/tag/x/page/2/">次へ</a>
    </div></nav>
  </div>
</body></html>
"""


def test_listing_uses_featured_img_link():
    scraper = Scraper(Config())
    links = scraper._extract_post_links(
        FEATURED_LISTING_HTML, "https://www.bigboobsjapan.com/tag/x/")
    assert links == [
        "https://www.bigboobsjapan.com/2020/01/15/set-a/",
        "https://www.bigboobsjapan.com/2021/06/02/set-b/",
    ]


def test_listing_urls_include_uppercase_tag(monkeypatch):
    scraper = Scraper(Config())
    monkeypatch.setattr(scraper, "_discover_taxonomy_urls", lambda name: [])
    plain = [u for u, _ in scraper._listing_urls("Shinozaki Ai")]
    # The proven pattern is the uppercased tag slug.
    assert "https://www.bigboobsjapan.com/tag/SHINOZAKI-AI/" in plain


# --------------------------------------------------------------------------
# cancellation (Stop button)
# --------------------------------------------------------------------------

def test_scraper_get_returns_none_when_cancelled():
    import threading
    event = threading.Event()
    event.set()
    scraper = Scraper(Config(obey_robots=False), cancel_event=event)
    # No HTTP is attempted once cancelled.
    assert scraper.get("https://www.bigboobsjapan.com/tag/x/") is None


def test_downloader_skips_all_when_cancelled(tmp_path, monkeypatch):
    import threading
    event = threading.Event()
    event.set()
    config = Config(output_dir=tmp_path, request_delay=0)
    downloader = Downloader(config, cancel_event=event)

    called = {"n": 0}

    def fake_fetch(url):
        called["n"] += 1
        return FakeResponse(content=b"x", headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr(downloader, "_fetch", fake_fetch)
    gallery = Gallery(url="https://x/s/", title="S",
                      images=["https://x/a.jpg", "https://x/b.jpg"])
    stats = downloader.download_gallery(gallery, tmp_path)
    # Nothing fetched or counted; work aborted immediately.
    assert called["n"] == 0
    assert (stats.downloaded, stats.skipped, stats.failed) == (0, 0, 0)


def test_run_cancelled_before_download_returns_empty(monkeypatch):
    import threading
    from bbjp_downloader import run
    event = threading.Event()
    event.set()

    # find_galleries returns some galleries, but the run must stop before
    # downloading because the event is already set.
    monkeypatch.setattr(
        "bbjp_downloader.Scraper.find_galleries",
        lambda self, name: [Gallery(url="https://x/s/", title="S",
                                    images=["https://x/a.jpg"])],
    )
    stats = run("whoever", Config(obey_robots=False), cancel_event=event)
    assert (stats.downloaded, stats.failed) == (0, 0)
