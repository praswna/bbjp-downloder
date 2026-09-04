# bbjp-downloader

Type a person's name and download all of their galleries from a WordPress
gallery site (defaults to `bigboobsjapan.com`). Ships with both a **command-line
interface** and a small **desktop GUI**.

> ⚠️ **Please read [Responsible use](#responsible-use) first.** This tool is for
> personal, lawful archiving only. You are responsible for complying with the
> target site's Terms of Service, its `robots.txt`, and applicable copyright
> law. Only ever use it for content depicting consenting adults.

---

## Features

- **Name → galleries → images.** Give it a name; it finds every matching
  gallery and downloads all the images in each.
- **Two discovery routes.** Tries the site's tag page (`/tag/<name>/`) first,
  then falls back to the built-in search (`/?s=<name>`), following pagination.
- **Full-resolution images.** Automatically upgrades WordPress' resized
  thumbnails (`photo-1024x768.jpg`, `photo-scaled.jpg`) to the original file.
- **Polite by default.** Global request throttling, retries with exponential
  backoff, a realistic User-Agent, and `robots.txt` awareness.
- **Resumable.** Already-downloaded files are skipped, so interrupted runs pick
  up where they left off.
- **Organised output.** `downloads/<name>/<gallery title>/0001_<image>.jpg`.
- **Configurable & robust.** CSS selectors, delays, concurrency, and the base
  URL are all overridable, with generic fallbacks if the site's theme changes.

## Installation

Requires Python 3.9+.

```bash
git clone <this-repo-url>
cd bbjp-downloder

# Option A: just the runtime deps
pip install -r requirements.txt

# Option B: install as a package (adds the `bbjp-downloader` command)
pip install -e .
```

Dependencies: [`requests`](https://pypi.org/project/requests/) and
[`beautifulsoup4`](https://pypi.org/project/beautifulsoup4/). The GUI uses
Tkinter, which ships with most CPython installs.

## Usage

### Command line

```bash
# Download every gallery for a name
python -m bbjp_downloader "Some Name"

# ...or, if installed as a package:
bbjp-downloader "Some Name"

# Just list what would be downloaded (no files written)
bbjp-downloader "Some Name" --list

# Choose an output folder, limit galleries, tune politeness/speed
bbjp-downloader "Some Name" -o ./out --limit 10 --delay 2 --workers 3

# See progress
bbjp-downloader "Some Name" -v
```

Common options:

| Option | Description | Default |
| --- | --- | --- |
| `-o, --output DIR` | Where to save images | `./downloads` |
| `-j, --workers N` | Concurrent image downloads | `4` |
| `--delay SECONDS` | Minimum gap between HTTP requests | `1.0` |
| `--limit N` | Maximum number of galleries | all |
| `--overwrite` | Re-download existing files | off |
| `--no-full-size` | Keep resized images, don't fetch originals | off |
| `--ignore-robots` | Skip the `robots.txt` check | off |
| `--base-url URL` | Point at a different site | `bigboobsjapan.com` |
| `--list` | List galleries only, don't download | — |
| `--gui` | Launch the graphical interface | — |
| `-v, -vv` | More verbose logging | — |

### Graphical interface

```bash
python -m bbjp_downloader --gui
```

Enter a name, pick a destination folder, adjust workers/delay if you like, and
press **Download**. Progress streams into the log panel; the window stays
responsive because the work runs on a background thread.

### As a library

```python
from bbjp_downloader import Config, run

stats = run("Some Name", Config(output_dir="out", request_delay=1.5))
print(stats.downloaded, "images downloaded")
```

You can also drive the pieces directly:

```python
from bbjp_downloader import Config, Scraper, Downloader

config = Config()
scraper = Scraper(config)
galleries = scraper.find_galleries("Some Name")   # discovery
Downloader(config).download_all(galleries, "Some Name")  # download
```

## How it works

1. **Discovery** (`scraper.py`): the name is slugified and requested as a tag
   page; failing that, as a search query. Listing pages are paginated
   (`/page/N/`) and each result's post link is collected, filtering out
   taxonomy/feed/pagination links.
2. **Extraction**: each gallery post is fetched and its `.entry-content` is
   scanned for images — both full-size `<a href="…jpg">` links and `<img>` tags
   (including lazy-loaded `data-src`/`srcset`). Chrome (avatars, logos, ads) is
   filtered out and resized variants are upgraded to originals.
3. **Download** (`downloader.py`): images are fetched concurrently under a
   shared rate limit, written atomically via a `.part` temp file, and named
   `NNNN_<original>.ext`. Existing files are skipped.

If the site's theme changes, override the selectors on `Config`
(`listing_selectors`, `content_selectors`) rather than editing the code.

## Testing

```bash
pip install pytest
pytest
```

The tests use local HTML fixtures and stubbed HTTP, so they run offline.

## Responsible use

- This tool is intended for **personal, lawful use** — e.g. archiving content
  you are permitted to save.
- **Respect the site.** Keep the default delay (or increase it), don't run many
  workers, and leave `robots.txt` checking on unless you have a good reason.
- **Respect copyright and Terms of Service.** Downloaded images remain the
  property of their respective rights holders. Do not redistribute.
- **Adults only.** Use exclusively for content depicting consenting adults.

You are solely responsible for how you use this software.

## License

MIT
