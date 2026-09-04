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

## Windows: just double-click

No command line needed. In the project folder you'll find ready-to-run `.bat`
files — double-click one:

| File | What it does |
| --- | --- |
| **`bbjp-downloader.bat`** | Opens the graphical app (enter a name, pick a folder, Download). |
| **`download.bat`** | Asks for a name in a console window, then downloads. |
| **`setup.bat`** | Optional. Pre-installs everything so the first run is instant. |

On the **first** run the launcher automatically creates a local Python
environment (`.venv`) and installs the dependencies — this needs an internet
connection and takes a minute. After that it starts immediately. You only need
[Python 3](https://www.python.org/downloads/) installed (tick *"Add Python to
PATH"* in its installer).

## Usage

### Name or URL

You can pass **either a name or a gallery URL**:

- **A name** — the tool looks the person up. On this site each person has a
  *category* page whose slug mixes romaji and Japanese (e.g.
  `/category/miura-sakura-水卜さくら/`), so a typed name can't reproduce it
  exactly. The tool therefore searches the site, finds the matching
  `category`/`tag` page automatically, and falls back to search results.
- **A URL** — paste the person's category/tag page directly and it is scraped
  as-is. **This is the most reliable option**, especially for Japanese names:

  ```bash
  python -m bbjp_downloader "https://www.bigboobsjapan.com/category/miura-sakura-水卜さくら/"
  ```

  (In the GUI, paste the URL into the *Name / URL* box. Copy it straight from
  your browser's address bar — encoded forms like `…%e6%b0%b4…` work too.)

### Command line

```bash
# Download every gallery for a name (auto-finds the category page)
python -m bbjp_downloader "Some Name"

# ...or scrape a category/tag URL directly (most reliable)
python -m bbjp_downloader "https://www.bigboobsjapan.com/category/<name>/"

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
| `--obey-robots` | Honour `robots.txt` (off by default — see below) | off |
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
responsive because the work runs on a background thread. Press **Stop** at any
time to cancel — it finishes the file in flight and stops; everything already
downloaded is kept, and running again resumes (existing files are skipped).

On the command line, press **Ctrl+C** for the same graceful stop (a second
Ctrl+C forces an immediate quit).

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

## Avoiding rate limits / bans

Heavy scraping *can* get your IP temporarily rate-limited or blocked. This tool
is built to stay well under that line, and you can tune it further:

- **Global throttle.** Requests are spaced by `--delay` (default 1s) and that
  gap is enforced *across all workers*, so more workers make writing to disk
  faster without increasing the request rate. There's random jitter on top so
  the cadence doesn't look like a metronome.
- **Backs off on `429`.** If the server says "Too Many Requests" the tool waits
  (honouring the `Retry-After` header) instead of hammering it.
- **Realistic headers & retries** with exponential backoff on transient errors.

Practical tips:

- Leave `--delay` at 1s or raise it (`--delay 2`–`3`) for large batches.
- Keep `--workers` modest (2–4). It won't speed up requests anyway.
- Grab one person at a time rather than looping many back-to-back; take breaks.
- If you ever see `429` / rate-limit messages, stop for a while and use a bigger
  delay next time.

None of this is a guarantee — but at ~1 request/second it behaves like a slow
human browser, which is about as safe as scraping gets.

## How it works

1. **Discovery** (`scraper.py`): if you pass a URL it is scraped directly.
   Otherwise the name is looked up — first by searching the site and picking
   the `category`/`tag` page whose slug contains every part of the name (this
   is how romaji+Japanese slugs are matched), then by guessing
   `/category/<slug>/` and `/tag/<slug>/`, and finally the raw search results.
   Listing pages are paginated (`/page/N/`) and each result's post link is
   collected, filtering out taxonomy/feed/pagination links.
2. **Extraction**: each post's gallery items (`div.gallery-item img`, etc.) are
   read and the **largest `srcset` candidate** is taken for every image — the
   same approach as the reference implementation. If a page has no gallery
   blocks it falls back to every `<img>` in the content area (plus any direct
   `<a href="…jpg">` links). Thumbnails, avatars, logos and ads are filtered out.
3. **Download** (`downloader.py`): images are fetched concurrently under a
   shared rate limit, written atomically via a `.part` temp file, and named
   `NNNN_<original>.ext`. With `--full-size` (default on) it first tries the
   un-resized original and falls back to the exact page URL if that 404s, so
   downloads never fail just because the original isn't published. Existing
   files are skipped.

The listing and image selectors that match this specific site
(`a.entry-featured-img-link`, `div.gallery-item img`, the uppercase `/tag/NAME/`
route) were derived from a known-working scraper for it.

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
- **Respect the site.** Keep the default delay (or increase it) and don't run
  many workers. This site's `robots.txt` disallows the very paths the tool needs
  (`/tag/`, `/category/`, search), so the check is **off by default**; the
  built-in request throttling stays on. Enable the check with `--obey-robots` if
  you prefer, understanding it will stop the tool from finding galleries here.
- **Respect copyright and Terms of Service.** Downloaded images remain the
  property of their respective rights holders. Do not redistribute.
- **Adults only.** Use exclusively for content depicting consenting adults.

You are solely responsible for how you use this software.

## License

MIT
