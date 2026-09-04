"""Configuration for the bbjp-downloader.

All tunable behaviour lives here so the scraper/downloader stay policy-free and
easy to adjust when the target site changes its markup.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence

# The site is a WordPress gallery blog. Model names are exposed both as tags
# (``/tag/<slug>/``) and via the built-in search (``/?s=<query>``). We try the
# tag route first because it is the most precise, then fall back to search.
DEFAULT_BASE_URL = "https://www.bigboobsjapan.com"

# A realistic desktop User-Agent. Some CDNs reject the default requests UA.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# CSS selectors used to locate the *post/gallery* links on a listing page.
# Ordered from most to least specific; the first selector that yields links
# wins. The first two match this site's theme exactly (each post's featured
# image is an <a class="entry-featured-img-link"> inside .entry-featured-img-wrap);
# the rest are generic fallbacks so the tool survives theme changes.
DEFAULT_LISTING_SELECTORS: Sequence[str] = (
    "a.entry-featured-img-link",
    ".entry-featured-img-wrap a",
    "h2.entry-title a",
    "h3.entry-title a",
    ".entry-title a",
    "article a[rel=bookmark]",
    "article h2 a",
    "article .post-title a",
)

# CSS selectors that locate gallery images inside a post. This site renders its
# photo sets as WordPress gallery items whose <img> carries a srcset; the real
# full-size URL is the largest srcset candidate. Tried before the generic
# "every <img> in the content area" fallback.
DEFAULT_GALLERY_IMAGE_SELECTORS: Sequence[str] = (
    "div.gallery-item img",
    "figure.gallery-item img",
    "div.gallery-icon img",
    ".wp-block-gallery img",
    ".tiled-gallery img",
)

# CSS selectors used to locate the container holding a gallery's images.
DEFAULT_CONTENT_SELECTORS: Sequence[str] = (
    "div.entry-content",
    "article .entry-content",
    "div.post-content",
    "main article",
    "article",
)

# Selectors for the "next page" link when paginating listings.
DEFAULT_NEXT_PAGE_SELECTORS: Sequence[str] = (
    "a.next",
    "a.next.page-numbers",
    "a[rel=next]",
    ".nav-previous a",  # WordPress puts *older* posts under "previous"
    ".pagination a.next",
)

IMAGE_EXTENSIONS: Sequence[str] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".avif",
)

# Substrings that mark an image as chrome/ads rather than gallery content.
DEFAULT_IMAGE_BLOCKLIST: Sequence[str] = (
    "gravatar",
    "/emoji/",
    "wp-content/uploads/sites",  # network sub-site avatars, rarely content
    "/avatar",
    "logo",
    "icon",
    "banner",
    "/ads/",
    "advert",
    "sprite",
    "spacer",
    "loading",
    "placeholder",
    "1x1",
)


@dataclass(frozen=True)
class Config:
    """Runtime configuration for a scraping/download run."""

    base_url: str = DEFAULT_BASE_URL
    user_agent: str = DEFAULT_USER_AGENT
    output_dir: Path = Path("downloads")

    # Politeness / reliability
    request_delay: float = 1.0        # seconds to wait between HTTP requests
    timeout: float = 30.0             # per-request timeout in seconds
    retries: int = 3                  # retry attempts for a failed request
    max_workers: int = 4              # concurrent image downloads

    # Behaviour
    # robots.txt on this site disallows the /tag, /category and search paths the
    # downloader needs, so it is OFF by default (the tool is for personal,
    # rate-limited use). Turn it back on with obey_robots=True / --obey-robots.
    obey_robots: bool = False         # honour robots.txt (warn + skip if disallowed)
    full_size: bool = True            # strip WordPress -WxH suffixes to fetch originals
    overwrite: bool = False           # re-download files that already exist
    max_galleries: int | None = None  # cap number of galleries (None = all)
    max_pages: int = 200              # safety cap on listing pagination

    # Selectors (advanced — override only if the theme changes)
    listing_selectors: Sequence[str] = field(default=DEFAULT_LISTING_SELECTORS)
    gallery_image_selectors: Sequence[str] = field(
        default=DEFAULT_GALLERY_IMAGE_SELECTORS)
    content_selectors: Sequence[str] = field(default=DEFAULT_CONTENT_SELECTORS)
    next_page_selectors: Sequence[str] = field(default=DEFAULT_NEXT_PAGE_SELECTORS)
    image_blocklist: Sequence[str] = field(default=DEFAULT_IMAGE_BLOCKLIST)
    image_extensions: Sequence[str] = field(default=IMAGE_EXTENSIONS)

    def with_overrides(self, **kwargs) -> "Config":
        """Return a copy of this config with the given fields replaced."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        if "output_dir" in clean:
            clean["output_dir"] = Path(clean["output_dir"])
        return replace(self, **clean)
