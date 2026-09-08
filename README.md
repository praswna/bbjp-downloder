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
- **Modern Qt GUI.** A PySide6/Qt window shows every gallery as a tile in a
  responsive grid (thumbnail, image count, a Download button and a Link ↗
  button to open the page in your browser). Also has Download-all, Stop,
  Open-folder, **Batch download** (multiple people in one run), Settings and
  log copy. Falls back to a Tkinter UI if PySide6 isn't installed.
- **Smart discovery.** Finds the person's `category`/`tag` page automatically
  (even romaji+Japanese slugs), or scrape a pasted URL directly. If a name
  matches several different people (e.g. "Ogura"), you're asked which one
  before anything downloads.
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

Dependencies: [`requests`](https://pypi.org/project/requests/),
[`beautifulsoup4`](https://pypi.org/project/beautifulsoup4/),
[`pillow`](https://pypi.org/project/pillow/) (thumbnails),
[`PySide6`](https://pypi.org/project/PySide6/) (the Qt GUI) and
[`selenium`](https://pypi.org/project/selenium/) (browser mode — see below). If
PySide6 isn't installed the app falls back to a built-in Tkinter GUI; if
selenium isn't installed the tool still works over plain HTTP. So the CLI's
bare minimum is just `requests` + `beautifulsoup4`. Browser mode also needs
[Google Chrome](https://www.google.com/chrome/) installed (Selenium drives it;
no separate chromedriver download needed).

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
| `--browser` | Read pages via a real Chrome (Selenium) — bypasses blocks | off |
| `--show-browser` | Show the Chrome window (with `--browser`; hidden by default) | off |
| `--base-url URL` | Point at a different site | `bigboobsjapan.com` |
| `--list` | List galleries only, don't download | — |
| `--gui` | Launch the graphical interface | — |
| `-v, -vv` | More verbose logging | — |

### Graphical interface

```bash
python -m bbjp_downloader --gui
```

Enter a name (or paste a URL) and press **Search**. If the name matches more
than one person (common surnames like "Ogura" can match several different
models), a **"Which one did you mean?"** dialog lists everyone found — pick
the right one and it re-searches just them. Each matching gallery then appears
**immediately** as a tile in a **responsive grid** (it reflows the column
count as you resize the window) with its **thumbnail** and title; the image
count fills in afterwards on a background thread. Every tile has its own
**Download** button and a **Link ↗** button that opens the gallery page in your
default web browser — grab just the sets you want, or press **Download all**.

Toolbar & niceties:

- **Open folder** — reveal the save location in your file manager.
- **Batch download** — paste in a list of names/URLs (one per line) and the
  app searches and downloads every gallery for each of them in turn, into its
  own per-person folder. Useful for grabbing several people unattended.
- **Settings ⚙** — workers, request delay, full-size toggle, save location and
  browser mode live here (out of the main view).
- **Copy** / **Clear** the log; the log also resets on each new search.
- **Stop** cancels the current operation (including a batch run mid-way);
  files already downloaded are kept, and running again resumes (existing files
  are skipped).

The GUI is built with **PySide6/Qt** for a modern look (rounded cards, hover
states, a proper settings dialog). If PySide6 isn't installed it automatically
falls back to a simpler Tkinter window with the same features.

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

## Bypassing site blocks (browser mode)

Plain HTTP requests can get silently blocked by anti-bot / JavaScript
challenges — pages load empty, or don't load at all, even though the same URL
works fine in your actual browser. If that happens, turn on **browser mode**:
the tool drives a real Chrome window (via [Selenium](https://www.selenium.dev/))
to load every page, so it looks like an ordinary visitor instead of a script.
Downloads then reuse that Chrome session's cookies, so hot-link / referer
checks pass too.

Chrome runs **hidden (headless)** by default — nothing pops up on screen.

- **GUI**: browser mode is on by default (⚙ **Settings** → *Browser mode*).
  Turn it off there if you'd rather use plain HTTP, or untick *headless* if
  you want to actually watch the browser (useful for troubleshooting).
- **CLI**: pass `--browser`; add `--show-browser` if you want to see the window:

  ```bash
  python -m bbjp_downloader "Some Name" --browser
  ```

Requires `pip install selenium` and Google Chrome installed — Selenium 4
downloads a matching driver automatically. If selenium isn't installed, the
GUI's browser-mode checkbox is disabled and the CLI flag is simply ignored
(falls back to plain HTTP, same as before).

Under the hood this swaps out *only* the network transport
(`BrowserScraper` in `browser.py` overrides `Scraper.get()` to return a
Chrome-rendered page instead of an HTTP response) — every other piece of
proven logic (tag/category discovery, homepage-fallback detection,
pagination, gallery/srcset extraction) runs completely unchanged. Browser
mode is slower per page (Chrome has to actually load and render), so it's
worth trying plain HTTP first and only switching on when you hit a block.

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
