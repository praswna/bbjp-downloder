"""bbjp-downloader — download all galleries for a named person.

Public API::

    from bbjp_downloader import Config, Scraper, Downloader, run

    stats = run("Some Name", Config(output_dir="out"))
"""

from __future__ import annotations

import logging

from .config import Config
from .downloader import Downloader, DownloadStats
from .scraper import Gallery, Scraper, slugify

__all__ = [
    "Config",
    "Scraper",
    "Downloader",
    "DownloadStats",
    "Gallery",
    "slugify",
    "run",
]

__version__ = "1.0.0"

logger = logging.getLogger(__name__)


def run(name: str, config: Config | None = None,
        progress=None) -> DownloadStats:
    """Find and download every gallery for ``name``.

    Parameters
    ----------
    name:
        The person's name to search for.
    config:
        Optional :class:`Config`; defaults are used when omitted.
    progress:
        Optional callable ``progress(event: str, **data)`` for UI hooks. Events:
        ``"search"``, ``"galleries"``, ``"gallery"``, ``"done"``.
    """
    config = config or Config()
    session = _build_session(config)
    scraper = Scraper(config, session=session)

    _emit(progress, "search", name=name)
    logger.info("Searching galleries for %r …", name)
    galleries = scraper.find_galleries(name)
    _emit(progress, "galleries", count=len(galleries), galleries=galleries)

    if not galleries:
        logger.warning("No galleries with images found for %r.", name)
        return DownloadStats()

    logger.info("Found %d gallery(ies); downloading …", len(galleries))
    downloader = Downloader(config, session=session)
    stats = DownloadStats()
    root = config.output_dir / _safe(name)
    root.mkdir(parents=True, exist_ok=True)
    for i, gallery in enumerate(galleries, 1):
        _emit(progress, "gallery", index=i, total=len(galleries), gallery=gallery)
        stats.merge(downloader.download_gallery(gallery, root))

    _emit(progress, "done", stats=stats)
    logger.info(
        "Done: %d downloaded, %d skipped, %d failed (%.1f MB).",
        stats.downloaded, stats.skipped, stats.failed,
        stats.bytes_written / 1_048_576,
    )
    return stats


def _build_session(config: Config):
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": config.user_agent,
        "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
    })
    return session


def _safe(name: str) -> str:
    from .downloader import sanitize_filename

    return sanitize_filename(name, "model")


def _emit(progress, event, **data):
    if progress is not None:
        try:
            progress(event, **data)
        except Exception:  # UI callbacks must never break a run
            logger.debug("progress callback raised", exc_info=True)
