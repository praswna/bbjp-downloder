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
