"""Scraping logic: turn a person's name into a list of galleries and images.

The site is a WordPress gallery blog, so we lean on WordPress conventions:

* Model names are reachable as tags (``/tag/<slug>/``) and via search
  (``/?s=<query>``), both paginated with ``/page/<n>/``.
* Each result item links to a *post* — one gallery/photo set.
* A post's images live inside an ``.entry-content`` container, either as
  ``<a href="…full.jpg">`` links or ``<img>`` tags (possibly lazy-loaded).

Everything here is defensive: selectors are tried in order and fall back to
generic heuristics, so small theme changes do not break the tool.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable, Iterator
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .config import Config

logger = logging.getLogger(__name__)

# Matches WordPress' resized-image suffix, e.g. "photo-1024x768.jpg".
_RESIZE_SUFFIX = re.compile(r"-\d{2,5}x\d{2,5}(?=\.[A-Za-z0-9]+$)")
# Matches "...-scaled.jpg" — WP's big-image variant; the original drops it.
_SCALED_SUFFIX = re.compile(r"-scaled(?=\.[A-Za-z0-9]+$)")


@dataclass
class Gallery:
    """A single gallery/post and the images discovered inside it."""

    url: str
    title: str
    images: list[str]

    @property
    def slug(self) -> str:
        path = urlparse(self.url).path.strip("/")
        return path.rsplit("/", 1)[-1] or "gallery"


def slugify(name: str) -> str:
    """Turn a display name into a WordPress-style tag slug.

    Non-ASCII (e.g. Japanese) names are kept as-is except for spaces, which
    WordPress joins with hyphens; ASCII is lowercased. WordPress URL-encodes
    non-ASCII slugs itself, so we leave the raw characters here and encode at
    request time.
    """
    name = name.strip().lower()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w\-]", "", name, flags=re.UNICODE)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name


class Scraper:
    """Discovers galleries and image URLs for a given person."""

    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", config.user_agent)
        self._robots: RobotFileParser | None = None
        self._last_request = 0.0

    # ---- HTTP helpers -----------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.config.request_delay - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, url: str) -> requests.Response | None:
        """GET a URL with throttling, retries and robots.txt awareness."""
        if not self._allowed_by_robots(url):
            logger.warning("robots.txt disallows %s — skipping", url)
            return None

        last_exc: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.config.timeout)
                if resp.status_code == 404:
                    logger.debug("404 for %s", url)
                    return None
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:  # noqa: PERF203
                last_exc = exc
                backoff = min(2 ** attempt, 30)
                logger.debug(
                    "request failed (%s/%s) for %s: %s — retrying in %ss",
                    attempt, self.config.retries, url, exc, backoff,
                )
                time.sleep(backoff)
        logger.error("giving up on %s: %s", url, last_exc)
        return None

    # ---- robots.txt -------------------------------------------------------

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.config.obey_robots:
            return True
        if self._robots is None:
            self._robots = self._load_robots()
        if self._robots is None:  # couldn't load — fail open
            return True
        return self._robots.can_fetch(self.config.user_agent, url)

    def _load_robots(self) -> RobotFileParser | None:
        robots_url = urljoin(self.config.base_url, "/robots.txt")
        parser = RobotFileParser()
        try:
            resp = self.session.get(robots_url, timeout=self.config.timeout)
            if resp.status_code >= 400:
                return None
            parser.parse(resp.text.splitlines())
            return parser
        except requests.RequestException:
            return None

    # ---- gallery discovery ------------------------------------------------

    def find_galleries(self, name: str) -> list[Gallery]:
        """Return every gallery whose listing matches ``name``.

        Tries the tag route first, then search, deduplicating post URLs. Images
        are extracted lazily by the caller via :meth:`extract_images`, but this
        method returns fully-populated :class:`Gallery` objects for convenience.
        """
        post_urls = self.find_gallery_urls(name)
        galleries: list[Gallery] = []
        for url in post_urls:
            gallery = self.extract_images(url)
            if gallery and gallery.images:
                galleries.append(gallery)
                logger.info(
                    "  %s — %d image(s)", gallery.title or gallery.slug,
                    len(gallery.images),
                )
            else:
                logger.info("  %s — no images found", url)
            if (self.config.max_galleries is not None
                    and len(galleries) >= self.config.max_galleries):
                break
        return galleries

    def find_gallery_urls(self, name: str) -> list[str]:
        """Collect post URLs for a name via tag pages, then search."""
        slug = slugify(name)
        seen: set[str] = set()
        ordered: list[str] = []

        for listing_url in self._listing_urls(name, slug):
            found_on_page = 0
            for page_url in self._paginate(listing_url):
                resp = self.get(page_url)
                if resp is None:
                    break
                links = self._extract_post_links(resp.text, page_url)
                new = [u for u in links if u not in seen]
                for u in new:
                    seen.add(u)
                    ordered.append(u)
                found_on_page += len(new)
                logger.info(
                    "listing %s → %d new gallery link(s) (%d total)",
                    page_url, len(new), len(ordered),
                )
                # Stop when a page is empty, or yields nothing new (a
                # last-page redirect back to page 1 repeats known links).
                if not links or not new:
                    break
                if (self.config.max_galleries is not None
                        and len(ordered) >= self.config.max_galleries):
                    return ordered[: self.config.max_galleries]
            # If the tag route produced results, don't bother with search.
            if found_on_page and listing_url.endswith("/"):
                break
        return ordered

    def _listing_urls(self, name: str, slug: str) -> list[str]:
        base = self.config.base_url.rstrip("/")
        urls = []
        if slug:
            # WordPress encodes non-ASCII slugs in the path.
            urls.append(f"{base}/tag/{quote_plus(slug)}/")
        urls.append(f"{base}/?s={quote_plus(name)}")
        return urls

    def _paginate(self, listing_url: str) -> Iterator[str]:
        """Yield successive pages of a listing URL (WordPress /page/N/ style)."""
        parsed = urlparse(listing_url)
        for page in range(1, self.config.max_pages + 1):
            if page == 1:
                yield listing_url
                continue
            if parsed.query:  # search URL: /page/N/?s=...
                base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
                yield f"{base}/page/{page}/?{parsed.query}"
            else:             # tag URL: /tag/slug/page/N/
                base = listing_url.rstrip("/")
                yield f"{base}/page/{page}/"

    def _extract_post_links(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        seen: set[str] = set()

        def add(href: str | None) -> None:
            if not href:
                return
            absolute = urljoin(page_url, href)
            if self._looks_like_post(absolute) and absolute not in seen:
                seen.add(absolute)
                links.append(absolute)

        for selector in self.config.listing_selectors:
            for a in soup.select(selector):
                add(a.get("href"))
            if links:
                return links

        # Fallback: any <article> descendant link that looks like a post.
        for article in soup.find_all("article"):
            for a in article.find_all("a", href=True):
                add(a["href"])
        return links

    @staticmethod
    def _host(netloc: str) -> str:
        host = netloc.lower().split("@")[-1].split(":")[0]
        return host[4:] if host.startswith("www.") else host

    def _looks_like_post(self, url: str) -> bool:
        """Heuristic: is this a single-post URL rather than nav/taxonomy?"""
        parsed = urlparse(url)
        base_host = self._host(urlparse(self.config.base_url).netloc)
        if parsed.netloc and self._host(parsed.netloc) != base_host:
            return False
        path = parsed.path.strip("/")
        if not path:
            return False
        # Exclude taxonomy / feed / author / pagination / attachment pages.
        blocked = (
            "tag/", "category/", "author/", "page/", "wp-", "feed",
            "comment", "#", "?replytocom", "/amp",
        )
        low = url.lower()
        if any(b in low for b in blocked):
            return False
        # Posts usually have a slug segment; exclude bare image links.
        if any(path.lower().endswith(ext) for ext in self.config.image_extensions):
            return False
        return True

    # ---- image extraction -------------------------------------------------

    def extract_images(self, gallery_url: str) -> Gallery | None:
        resp = self.get(gallery_url)
        if resp is None:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        title = self._extract_title(soup) or gallery_url
        container = self._find_content(soup)
        scope = container or soup

        images: list[str] = []
        seen: set[str] = set()

        def add(url: str | None) -> None:
            if not url:
                return
            absolute = urljoin(gallery_url, url.strip())
            absolute = self._normalise_image(absolute)
            if not absolute or absolute in seen:
                return
            if not self._is_content_image(absolute):
                return
            seen.add(absolute)
            images.append(absolute)

        # 1) Anchor links pointing directly at full-resolution images.
        for a in scope.find_all("a", href=True):
            href = a["href"]
            if any(href.lower().split("?")[0].endswith(ext)
                   for ext in self.config.image_extensions):
                add(href)

        # 2) <img> tags (including common lazy-load attributes).
        for img in scope.find_all("img"):
            candidate = (
                img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
                or self._largest_from_srcset(img.get("srcset"))
                or img.get("src")
            )
            add(candidate)

        return Gallery(url=gallery_url, title=title, images=images)

    def _extract_title(self, soup: BeautifulSoup) -> str | None:
        for selector in ("h1.entry-title", ".entry-title", "h1", "title"):
            el = soup.select_one(selector)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return None

    def _find_content(self, soup: BeautifulSoup):
        for selector in self.config.content_selectors:
            el = soup.select_one(selector)
            if el:
                return el
        return None

    def _normalise_image(self, url: str) -> str | None:
        """Optionally upgrade a resized WordPress image to its original."""
        if not self.config.full_size:
            return url
        upgraded = _SCALED_SUFFIX.sub("", _RESIZE_SUFFIX.sub("", url))
        return upgraded

    def _is_content_image(self, url: str) -> bool:
        low = url.lower().split("?")[0]
        if not any(low.endswith(ext) for ext in self.config.image_extensions):
            return False
        return not any(bad in url.lower() for bad in self.config.image_blocklist)

    @staticmethod
    def _largest_from_srcset(srcset: str | None) -> str | None:
        """Pick the highest-resolution candidate from a srcset attribute."""
        if not srcset:
            return None
        best_url, best_w = None, -1.0
        for part in srcset.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            url = bits[0]
            width = 0.0
            if len(bits) > 1 and bits[1].endswith("w"):
                try:
                    width = float(bits[1][:-1])
                except ValueError:
                    width = 0.0
            if width >= best_w:
                best_w, best_url = width, url
        return best_url
