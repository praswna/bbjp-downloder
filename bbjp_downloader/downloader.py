"""Downloading logic: fetch a gallery's images to disk, politely and resumably.

Downloads are:

* **Foldered** per model and per gallery so runs stay organised.
* **Resumable** — an existing, non-empty file is skipped unless ``overwrite``.
* **Concurrent** but bounded by ``max_workers``, each worker sharing the same
  throttled session so we never hammer the server.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from .config import Config
from .scraper import (
    _RESIZE_SUFFIX,
    _SCALED_SUFFIX,
    _retry_after_seconds,
    Gallery,
)

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Map common content-types to extensions when a URL has none.
_CT_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Make a string safe to use as a file or directory name."""
    name = unquote(name).strip()
    name = _UNSAFE.sub("_", name)
    name = re.sub(r"_{2,}", "_", name)
    name = name.strip("_ .")
    return name[:180] or fallback


@dataclass
class DownloadStats:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_written: int = 0
    failures: list[str] = field(default_factory=list)

    def merge(self, other: "DownloadStats") -> None:
        self.downloaded += other.downloaded
        self.skipped += other.skipped
        self.failed += other.failed
        self.bytes_written += other.bytes_written
        self.failures.extend(other.failures)


class Downloader:
    """Downloads galleries to disk with throttling and resume support."""

    def __init__(self, config: Config, session: requests.Session | None = None,
                 cancel_event: threading.Event | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", config.user_agent)
        self.cancel_event = cancel_event
        self._rate_lock = threading.Lock()
        self._last_request = 0.0

    # ---- throttled fetch --------------------------------------------------

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _throttle(self) -> None:
        # Serialise the *inter-request gap* across worker threads so overall
        # request rate stays bounded even with concurrency, plus jitter so the
        # cadence isn't a regular bot-like tick.
        with self._rate_lock:
            target = self.config.request_delay
            if target > 0:
                target += random.uniform(0, target * 0.5)
            wait = target - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _fetch(self, url: str) -> requests.Response | None:
        last_exc: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            if self._cancelled():
                return None
            self._throttle()
            try:
                resp = self.session.get(
                    url, timeout=self.config.timeout, stream=True,
                    headers={"Referer": self.config.base_url},
                )
                if resp.status_code == 429:   # Too Many Requests — back off
                    delay = _retry_after_seconds(resp, attempt)
                    resp.close()
                    logger.warning("rate limited (429) — waiting %.0fs", delay)
                    time.sleep(delay)
                    continue
                # Don't waste retries on a genuine client error (e.g. a 404
                # when probing for a full-size original) — bail out at once.
                if 400 <= resp.status_code < 500:
                    resp.close()
                    return None
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 30))
        logger.debug("failed to fetch %s: %s", url, last_exc)
        return None

    def _full_size_candidates(self, url: str) -> list[str]:
        """URLs to try for one image: the un-resized original first (when
        ``full_size`` is on), then the exact URL the page gave us."""
        if not self.config.full_size:
            return [url]
        stripped = _SCALED_SUFFIX.sub("", _RESIZE_SUFFIX.sub("", url))
        return [stripped, url] if stripped != url else [url]

    # ---- public API -------------------------------------------------------

    def download_gallery(self, gallery: Gallery, dest_root: Path) -> DownloadStats:
        """Download every image in ``gallery`` into ``dest_root/<gallery>/``."""
        folder_name = sanitize_filename(
            gallery.title if gallery.title != gallery.url else gallery.slug,
            fallback=gallery.slug,
        )
        dest = dest_root / folder_name
        dest.mkdir(parents=True, exist_ok=True)

        stats = DownloadStats()
        total = len(gallery.images)
        pad = len(str(total))

        def task(index_url):
            index, url = index_url
            if self._cancelled():           # already-queued work bails out fast
                return "cancelled", 0
            base = f"{index + 1:0{pad}d}"
            return self._download_one(url, dest, base)

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = {
                pool.submit(task, (i, url)): url
                for i, url in enumerate(gallery.images)
            }
            for future in as_completed(futures):
                result, nbytes = future.result()
                if result == "downloaded":
                    stats.downloaded += 1
                    stats.bytes_written += nbytes
                elif result == "skipped":
                    stats.skipped += 1
                elif result == "cancelled":
                    pass                    # not counted; work was aborted
                else:
                    stats.failed += 1
                    stats.failures.append(futures[future])
        logger.info(
            "  → %s: %d downloaded, %d skipped, %d failed",
            folder_name, stats.downloaded, stats.skipped, stats.failed,
        )
        return stats

    def download_all(self, galleries, person: str) -> DownloadStats:
        """Download all galleries for a person under ``output_dir/<person>/``."""
        root = self.config.output_dir / sanitize_filename(person, "model")
        root.mkdir(parents=True, exist_ok=True)
        total = DownloadStats()
        for i, gallery in enumerate(galleries, 1):
            if self._cancelled():
                logger.info("cancelled — stopping downloads")
                break
            logger.info(
                "[%d/%d] %s (%d images)",
                i, len(galleries), gallery.title or gallery.slug,
                len(gallery.images),
            )
            total.merge(self.download_gallery(gallery, root))
        return total

    # ---- single-file download --------------------------------------------

    def _download_one(self, url: str, dest: Path, base_name: str):
        """Download one image. Returns (status, bytes) where status is one of
        'downloaded' | 'skipped' | 'failed'."""
        url_name = sanitize_filename(Path(urlparse(url).path).name, base_name)
        # Preserve URL's extension; may be corrected after we see content-type.
        stem, ext = os.path.splitext(url_name)
        filename = f"{base_name}_{stem}{ext}" if stem else f"{base_name}{ext}"
        target = dest / filename

        if target.exists() and target.stat().st_size > 0 and not self.config.overwrite:
            return "skipped", 0

        # Try the full-size original first, then fall back to the page's URL.
        resp = None
        for candidate in self._full_size_candidates(url):
            resp = self._fetch(candidate)
            if resp is not None:
                break
        if resp is None:
            return "failed", 0
        try:
            if not ext:
                ct = resp.headers.get("Content-Type", "").split(";")[0].strip()
                target = target.with_suffix(_CT_EXT.get(ct, ".jpg"))
            tmp = target.with_suffix(target.suffix + ".part")
            nbytes = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
                        nbytes += len(chunk)
            if nbytes == 0:
                tmp.unlink(missing_ok=True)
                return "failed", 0
            tmp.replace(target)
            return "downloaded", nbytes
        except OSError as exc:
            logger.debug("write error for %s: %s", url, exc)
            return "failed", 0
        finally:
            resp.close()
