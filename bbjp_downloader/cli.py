"""Command-line interface for bbjp-downloader."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__, run
from .config import Config
from .scraper import Scraper


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bbjp-downloader",
        description=(
            "Download all galleries for a named person from a WordPress "
            "gallery site. For personal, lawful use only — respect the site's "
            "terms of service, robots.txt and copyright."
        ),
    )
    p.add_argument("name", nargs="?", help="Person/model name to search for.")
    p.add_argument("-o", "--output", metavar="DIR", default=None,
                   help="Output directory (default: ./downloads).")
    p.add_argument("--base-url", default=None,
                   help="Override the site base URL.")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="Concurrent image downloads (default: 4).")
    p.add_argument("--delay", type=float, default=None,
                   help="Seconds between HTTP requests (default: 1.0).")
    p.add_argument("--limit", type=int, default=None, dest="max_galleries",
                   help="Maximum number of galleries to download.")
    p.add_argument("--overwrite", action="store_true", default=None,
                   help="Re-download files that already exist.")
    p.add_argument("--no-full-size", dest="full_size", action="store_false",
                   default=None,
                   help="Keep resized images instead of upgrading to originals.")
    p.add_argument("--ignore-robots", dest="obey_robots", action="store_false",
                   default=None,
                   help="Do not consult robots.txt (use responsibly).")
    p.add_argument("--list", action="store_true",
                   help="Only list matching galleries; do not download.")
    p.add_argument("--gui", action="store_true",
                   help="Launch the graphical interface instead of the CLI.")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="Increase logging verbosity (-v, -vv).")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    return p


def _config_from_args(args) -> Config:
    return Config().with_overrides(
        output_dir=args.output,
        base_url=args.base_url,
        max_workers=args.workers,
        request_delay=args.delay,
        max_galleries=args.max_galleries,
        overwrite=args.overwrite,
        full_size=args.full_size,
        obey_robots=args.obey_robots,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(
        level=level, format="%(message)s", stream=sys.stderr,
    )
    logging.getLogger("bbjp_downloader").setLevel(
        logging.INFO if args.verbose == 0 else level
    )

    if args.gui:
        from .gui import launch
        return launch(_config_from_args(args))

    if not args.name:
        build_parser().error("a name is required (or use --gui)")

    config = _config_from_args(args)

    if args.list:
        scraper = Scraper(config)
        galleries = scraper.find_galleries(args.name)
        if not galleries:
            print(f"No galleries found for {args.name!r}.")
            return 1
        print(f"Found {len(galleries)} gallery(ies) for {args.name!r}:\n")
        for g in galleries:
            print(f"  • {g.title or g.slug}  ({len(g.images)} images)")
            print(f"    {g.url}")
        return 0

    stats = run(args.name, config)
    print(
        f"\nDone: {stats.downloaded} downloaded, {stats.skipped} skipped, "
        f"{stats.failed} failed ({stats.bytes_written / 1_048_576:.1f} MB)."
    )
    if stats.failed and stats.downloaded == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
