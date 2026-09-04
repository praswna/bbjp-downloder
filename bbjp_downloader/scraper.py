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
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Iterator
from urllib.parse import quote, quote_plus, unquote, urljoin, urlparse
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


@dataclass
class GalleryStub:
    """Lightweight listing entry (title + featured thumbnail) taken straight
    from a listing page, so the UI can show a card before the full post is
    fetched. Call :meth:`Scraper.extract_images` on ``url`` for the images."""

    url: str
    title: str
    thumb: str | None = None

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


def _retry_after_seconds(resp, attempt: int) -> float:
    """How long to wait after an HTTP 429, honouring ``Retry-After`` if given.

    Falls back to an exponential back-off (5s, 10s, 20s, …) capped at 120s.
    """
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return max(1.0, min(float(header), 120.0))
        except ValueError:
            pass
    return float(min(5 * 2 ** (attempt - 1), 120))


def is_url(text: str) -> bool:
    """True if ``text`` looks like an http(s) URL rather than a bare name."""
    try:
        parsed = urlparse(text.strip())
    except (ValueError, AttributeError):
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def person_label(name: str) -> str:
    """A tidy label for output folders, handling pasted listing URLs.

    For a URL we use its final (decoded) path segment — e.g. a category URL
    ``…/category/miura-sakura-水卜さくら/`` becomes ``miura-sakura-水卜さくら``.
    """
    if is_url(name):
        path = urlparse(name.strip()).path.rstrip("/")
        segment = unquote(path.rsplit("/", 1)[-1]) if path else ""
        return segment or urlparse(name.strip()).netloc
    return name


class Scraper:
    """Discovers galleries and image URLs for a given person."""

    def __init__(self, config: Config, session: requests.Session | None = None,
                 cancel_event: "threading.Event | None" = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", config.user_agent)
        self.cancel_event = cancel_event
        self._robots: RobotFileParser | None = None
        self._last_request = 0.0
        self._home_posts: set[str] | None = None  # homepage latest-post links

    # ---- HTTP helpers -----------------------------------------------------

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _throttle(self) -> None:
        # Base delay plus random jitter, so the request cadence isn't a
        # perfectly regular (bot-like) tick that's easy to rate-limit.
        target = self.config.request_delay
        if target > 0:
            target += random.uniform(0, target * 0.5)
        wait = target - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, url: str) -> requests.Response | None:
        """GET a URL with throttling, retries and robots.txt awareness."""
        if self._cancelled():
            return None
        if not self._allowed_by_robots(url):
            logger.warning("robots.txt disallows %s — skipping", url)
            return None

        last_exc: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.config.timeout)
                if resp.status_code == 429:   # Too Many Requests — back off
                    delay = _retry_after_seconds(resp, attempt)
                    logger.warning(
                        "rate limited (429) on %s — waiting %.0fs", url, delay)
                    time.sleep(delay)
                    continue
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
        stubs = self._collect_listing_items(name)
        galleries: list[Gallery] = []
        for stub in stubs:
            if self._cancelled():
                logger.info("cancelled — stopping gallery scan")
                break
            gallery = self.extract_images(stub.url)
            if gallery and gallery.images:
                galleries.append(gallery)
                logger.info(
                    "  %s — %d image(s)", gallery.title or gallery.slug,
                    len(gallery.images),
                )
            else:
                logger.info("  %s — no images found", stub.url)
            if (self.config.max_galleries is not None
                    and len(galleries) >= self.config.max_galleries):
                break
        return galleries

    def find_gallery_stubs(self, name: str) -> list[GalleryStub]:
        """Return listing entries (title + thumbnail + URL) without fetching
        each post — fast enough to render a UI immediately."""
        return self._collect_listing_items(name)

    def find_gallery_urls(self, name: str) -> list[str]:
        """Collect gallery/post URLs for a name — or a pasted listing URL."""
        return [stub.url for stub in self._collect_listing_items(name)]

    def _collect_listing_items(self, name: str) -> list[GalleryStub]:
        """Walk the listing candidates and gather de-duplicated gallery stubs.

        If ``name`` is a URL it is scraped directly. Otherwise we try, in order:
        category/tag links discovered from search, then ``/category/<slug>/``
        and ``/tag/<slug>/`` guesses, then raw search — stopping as soon as an
        authoritative listing yields galleries.
        """
        seen: set[str] = set()
        ordered: list[GalleryStub] = []

        for listing_url, authoritative, is_guess in self._listing_urls(name):
            if self._cancelled():
                break
            found_here = 0
            for page_index, page_url in enumerate(self._paginate(listing_url)):
                resp = self.get(page_url)
                if resp is None:
                    break
                items = self._extract_listing_items(resp.text, page_url)
                links = [it.url for it in items]
                # This site doesn't 404 for a missing tag/category or for a
                # page past the last one — it shows the homepage's latest
                # posts. Detect that and stop, discarding the page's links:
                #   * page 1 of a *guessed* URL → the archive doesn't exist
                #   * any later page → we've paginated past the real content
                # The first page of a discovered/real URL is trusted (a busy
                # model's newest posts can legitimately match the homepage).
                if (links and (is_guess or page_index > 0)
                        and self._is_homepage_fallback(links)):
                    where = "no real archive" if page_index == 0 else "end of archive"
                    logger.info(
                        "%s → homepage fallback (%s) — stopping",
                        page_url, where,
                    )
                    break
                new = [it for it in items if it.url not in seen]
                for it in new:
                    seen.add(it.url)
                    ordered.append(it)
                found_here += len(new)
                logger.info(
                    "listing %s → %d new gallery link(s) (%d total)",
                    page_url, len(new), len(ordered),
                )
                # Stop when a page is empty, or yields nothing new (a
                # last-page redirect back to page 1 repeats known links).
                if not items or not new:
                    break
                if (self.config.max_galleries is not None
                        and len(ordered) >= self.config.max_galleries):
                    return ordered[: self.config.max_galleries]
            # A category/tag/explicit-URL listing names exactly one person, so
            # once it produced galleries we don't dilute with fuzzy search.
            if found_here and authoritative:
                break
        return ordered

    def _listing_urls(self, name: str) -> list[tuple[str, bool, bool]]:
        """Ordered ``(url, authoritative, is_guess)`` listing candidates.

        ``authoritative`` marks category/tag/explicit-URL pages that list a
        single person's galleries; the first such page that works ends the hunt.
        ``is_guess`` marks constructed tag/category URLs that may not exist —
        those get a homepage-fallback check before their results are trusted.
        """
        # 1) A pasted URL is scraped directly — nothing else to try.
        if is_url(name):
            return [(name.strip(), True, False)]

        base = self.config.base_url.rstrip("/")
        candidates: list[tuple[str, bool, bool]] = []

        # 2) Real category/tag URLs discovered from the search page. Slugs here
        #    look like "miura-sakura-水卜さくら", which a typed name cannot
        #    reproduce, so we let the site hand us the exact URL. These are real
        #    links from the site, so they need no fallback check.
        for url in self._discover_taxonomy_urls(name):
            candidates.append((url, True, False))

        # 3) Direct guesses. The uppercase tag (e.g. /tag/SHINOZAKI-AI/) is the
        #    form this site actually uses for romaji names, so it comes first;
        #    lowercase category/tag variants cover other cases. Marked as
        #    guesses because a non-existent one silently shows recent posts.
        upper = quote(name.strip().upper().replace(" ", "-"), safe="-")
        if upper:
            candidates.append((f"{base}/tag/{upper}/", True, True))
        slug = slugify(name)
        if slug:
            enc = quote(slug, safe="-")  # keep hyphens; %-encode non-ASCII
            candidates.append((f"{base}/category/{enc}/", True, True))
            candidates.append((f"{base}/tag/{enc}/", True, True))

        # 4) Raw search results as a fuzzy last resort.
        candidates.append((f"{base}/?s={quote_plus(name)}", False, False))

        # De-duplicate, keeping order and the first flags seen.
        seen: set[str] = set()
        result: list[tuple[str, bool, bool]] = []
        for url, auth, guess in candidates:
            if url not in seen:
                seen.add(url)
                result.append((url, auth, guess))
        return result

    def _homepage_posts(self) -> set[str]:
        """Post links on the site homepage, cached — the set a non-existent
        tag/category page falls back to showing."""
        if self._home_posts is None:
            resp = self.get(self.config.base_url)
            self._home_posts = (
                set(self._extract_post_links(resp.text, self.config.base_url))
                if resp is not None else set()
            )
        return self._home_posts

    def _is_homepage_fallback(self, links: list[str]) -> bool:
        """True if ``links`` are essentially the homepage's latest posts, i.e.
        the page is a non-existent archive showing recent posts."""
        home = self._homepage_posts()
        if not home or not links:
            return False
        overlap = sum(1 for u in links if u in home)
        return overlap >= max(2, int(len(links) * 0.6))

    def _discover_taxonomy_urls(self, name: str) -> list[str]:
        """Find category/tag pages for ``name`` from the site's search results.

        Returns listing URLs whose decoded slug contains *every* token of the
        name, so "miura sakura" resolves to "/category/miura-sakura-水卜さくら/".
        """
        base = self.config.base_url.rstrip("/")
        resp = self.get(f"{base}/?s={quote_plus(name)}")
        if resp is None:
            return []
        tokens = [t for t in slugify(name).split("-") if t]
        if not tokens:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        base_host = self._host(urlparse(self.config.base_url).netloc)
        found: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"])
            parsed = urlparse(href)
            if parsed.netloc and self._host(parsed.netloc) != base_host:
                continue
            low = href.lower()
            if "/category/" not in low and "/tag/" not in low:
                continue
            decoded = unquote(low)
            normalised = href.rstrip("/") + "/"
            if all(tok in decoded for tok in tokens) and normalised not in seen:
                seen.add(normalised)
                found.append(normalised)
        if found:
            logger.info(
                "discovered %d matching category/tag page(s) for %r",
                len(found), name,
            )
        return found

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

    def _extract_listing_items(self, html: str, page_url: str) -> "list[GalleryStub]":
        """Extract per-post stubs (URL + title + featured thumbnail) from a
        listing page, so the UI can show cards without opening each post."""
        soup = BeautifulSoup(html, "html.parser")
        articles = soup.find_all("article")
        if not articles:
            # No article wrappers — fall back to URL-only stubs.
            return [GalleryStub(url=u, title=u)
                    for u in self._extract_post_links(html, page_url)]

        items: list[GalleryStub] = []
        seen: set[str] = set()
        for art in articles:
            a = (art.select_one("a.entry-featured-img-link")
                 or art.select_one(".entry-featured-img-wrap a")
                 or art.select_one(".entry-title a"))
            if a is None or not a.get("href"):
                continue
            url = urljoin(page_url, a["href"])
            if not self._looks_like_post(url) or url in seen:
                continue
            seen.add(url)

            img = (art.select_one(".entry-featured-img-wrap img")
                   or a.find("img") or art.find("img"))
            thumb = self._best_img_url(img) if img is not None else None
            if thumb:
                thumb = urljoin(page_url, thumb.strip())

            title_el = art.select_one(".entry-title")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title and img is not None:
                title = (img.get("alt") or "").strip()
            items.append(GalleryStub(url=url, title=title or url, thumb=thumb))

        if not items:  # articles present but none matched — degrade gracefully
            return [GalleryStub(url=u, title=u)
                    for u in self._extract_post_links(html, page_url)]
        return items

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

        # 1) Anchor links pointing directly at full-resolution image files
        #    (generic sites; on this theme the gallery <a> links to attachment
        #    pages, which don't end in an image extension and so are ignored).
        for a in scope.find_all("a", href=True):
            href = a["href"]
            if any(href.lower().split("?")[0].endswith(ext)
                   for ext in self.config.image_extensions):
                add(href)

        # 2) Gallery images. Prefer the theme's explicit gallery items; if the
        #    page has none, fall back to every <img> in the content area.
        img_tags = []
        for selector in self.config.gallery_image_selectors:
            img_tags = soup.select(selector)
            if img_tags:
                break
        if not img_tags:
            img_tags = scope.find_all("img")

        for img in img_tags:
            candidate = self._best_img_url(img)
            add(candidate)

        return Gallery(url=gallery_url, title=title, images=images)

    def _best_img_url(self, img) -> str | None:
        """Pick the best URL for an <img>, preferring the largest srcset.

        Mirrors the proven approach: take the highest-resolution srcset
        candidate, then fall back to lazy-load attributes and finally ``src``.
        """
        return (
            self._largest_from_srcset(img.get("srcset"))
            or self._largest_from_srcset(img.get("data-srcset"))
            or self._largest_from_srcset(img.get("data-lazy-srcset"))
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("src")
        )

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
        """Return the image URL as-is.

        We keep the exact (largest-srcset) URL the page provided, because it is
        guaranteed to exist. Upgrading to the un-resized original is done at
        download time (see ``Downloader``), which can fall back to this URL if
        the original 404s.
        """
        return url

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
