"""A polished Tkinter GUI: search, preview thumbnails, download per gallery.

Search lists each matching gallery as a card with a thumbnail, title, image
count and its own Download button; a Download-all button and a Stop button sit
in the toolbar. Network work runs on background threads and all widget updates
are marshalled back to the main thread, so the window stays responsive.

Thumbnails need Pillow (``pip install pillow``). Without it everything still
works — the cards just show a placeholder instead of an image.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
from pathlib import Path

import requests

from . import run
from .config import Config
from .downloader import Downloader, sanitize_filename
from .scraper import Scraper, person_label

try:  # thumbnails are optional
    from PIL import Image, ImageTk
    _HAVE_PIL = True
except Exception:  # pragma: no cover - depends on the environment
    _HAVE_PIL = False


# --- palette --------------------------------------------------------------
BG = "#f3f4f6"        # window background
CARD = "#ffffff"      # card background
BORDER = "#e5e7eb"    # hairline borders
INK = "#111827"       # primary text
MUTED = "#6b7280"     # secondary text
ACCENT = "#4f46e5"    # indigo
ACCENT_DK = "#4338ca"
DANGER = "#dc2626"
THUMB_W, THUMB_H = 116, 150


class _QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(self.format(record))


def launch(config: Config | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # pragma: no cover
        print(f"Tkinter is not available: {exc}")
        print('Use the command line instead, e.g.:  bbjp-downloader "Name"')
        return 1

    app = _App(tk, ttk, config or Config())
    app.root.mainloop()
    return 0


class _App:
    def __init__(self, tk, ttk, config: Config):
        self.tk = tk
        self.ttk = ttk
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.user_agent,
            "Referer": config.base_url,
        })
        # Separate session for thumbnail fetches (own thread pool).
        self.thumb_session = requests.Session()
        self.thumb_session.headers.update(dict(self.session.headers))

        self.cancel_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.galleries: list = []
        self.person: str = ""
        self._thumb_refs: list = []          # keep PhotoImage alive
        self._thumb_sema = threading.BoundedSemaphore(4)
        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._cards: list = []

        self._build()
        self._drain_log()

    # ---- UI construction --------------------------------------------------

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = tk.Tk()
        self.root = root
        root.title("BBJP Gallery Downloader")
        root.geometry("760x620")
        root.minsize(620, 520)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._init_style()

        # Header -----------------------------------------------------------
        header = tk.Frame(root, bg=ACCENT, height=58)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="  BBJP Gallery Downloader", bg=ACCENT,
                 fg="white", font=self.f_title).pack(side="left", pady=12)
        tk.Label(header, text="personal use · respect the site  ",
                 bg=ACCENT, fg="#c7d2fe", font=self.f_small).pack(
                     side="right", pady=18)

        # Search bar -------------------------------------------------------
        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=16, pady=(14, 6))

        self.name_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.name_var, font=self.f_body)
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        entry.bind("<Return>", lambda _e: self.search())
        entry.focus()

        self.search_btn = ttk.Button(bar, text="Search", style="Accent.TButton",
                                     command=self.search)
        self.search_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = ttk.Button(bar, text="Stop", style="Danger.TButton",
                                   command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        tk.Label(root, text="Enter a model name, or paste a …/category/<name>/ "
                 "URL, then press Search.", bg=BG, fg=MUTED,
                 font=self.f_small).pack(anchor="w", padx=18)

        # Options row ------------------------------------------------------
        opts = tk.Frame(root, bg=BG)
        opts.pack(fill="x", padx=16, pady=(8, 4))

        self.out_var = tk.StringVar(
            value=str(Path(self.config.output_dir).resolve()))
        tk.Label(opts, text="Save to:", bg=BG, fg=INK,
                 font=self.f_small).pack(side="left")
        ttk.Entry(opts, textvariable=self.out_var, font=self.f_small,
                  width=30).pack(side="left", padx=(4, 4))
        ttk.Button(opts, text="…", width=3, command=self._browse).pack(
            side="left")

        self.workers_var = tk.IntVar(value=self.config.max_workers)
        self.delay_var = tk.DoubleVar(value=self.config.request_delay)
        self.fullsize_var = tk.BooleanVar(value=self.config.full_size)
        tk.Label(opts, text="  Workers", bg=BG, fg=MUTED,
                 font=self.f_small).pack(side="left")
        ttk.Spinbox(opts, from_=1, to=16, width=3,
                    textvariable=self.workers_var).pack(side="left", padx=2)
        tk.Label(opts, text="Delay", bg=BG, fg=MUTED,
                 font=self.f_small).pack(side="left")
        ttk.Spinbox(opts, from_=0, to=10, increment=0.5, width=4,
                    textvariable=self.delay_var).pack(side="left", padx=2)
        ttk.Checkbutton(opts, text="Full-size", variable=self.fullsize_var).pack(
            side="left", padx=(6, 0))

        self.download_all_btn = ttk.Button(
            opts, text="Download all", style="Accent.TButton",
            command=self.download_all, state="disabled")
        self.download_all_btn.pack(side="right")

        # Results (scrollable cards) --------------------------------------
        wrap = tk.Frame(root, bg=BORDER, bd=0)
        wrap.pack(fill="both", expand=True, padx=16, pady=(6, 6))
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.results = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.results,
                                              anchor="nw")
        self.results.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(seq, self._on_wheel)

        self._empty = tk.Label(
            self.results, bg=BG, fg=MUTED, font=self.f_body, justify="center",
            text="\n\nNo results yet.\nSearch for a name to see galleries here.")
        self._empty.pack(pady=40)

        # Status + mini log ------------------------------------------------
        self.status_var = tk.StringVar(value="Ready.")
        status = tk.Frame(root, bg=BG)
        status.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(status, textvariable=self.status_var, bg=BG, fg=INK,
                 font=self.f_small, anchor="w").pack(side="left")
        if not _HAVE_PIL:
            tk.Label(status, text="(install Pillow for thumbnails)", bg=BG,
                     fg=MUTED, font=self.f_small).pack(side="right")

        self.log = tk.Text(root, height=4, wrap="word", bg="#111827",
                           fg="#d1d5db", bd=0, font=self.f_mono,
                           state="disabled", padx=8, pady=6)
        self.log.pack(fill="x", padx=16, pady=(0, 12))
        self._logln("Enter a name and press Search. For personal, lawful use "
                    "only — respect the site's terms, robots.txt and copyright.")

    def _init_style(self) -> None:
        import tkinter.font as tkfont
        self.f_title = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self.f_body = tkfont.Font(family="Segoe UI", size=11)
        self.f_bold = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.f_small = tkfont.Font(family="Segoe UI", size=9)
        self.f_mono = tkfont.Font(family="Consolas", size=9)

        style = self.ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", font=self.f_body, padding=(12, 6))
        style.configure("Accent.TButton", foreground="white",
                        background=ACCENT, borderwidth=0, padding=(14, 6))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DK), ("disabled", "#c7cbe8")])
        style.configure("Danger.TButton", foreground="white",
                        background=DANGER, borderwidth=0, padding=(14, 6))
        style.map("Danger.TButton",
                  background=[("active", "#b91c1c"), ("disabled", "#f0b4b4")])
        style.configure("Card.TButton", foreground="white",
                        background=ACCENT, borderwidth=0, padding=(10, 5))
        style.map("Card.TButton",
                  background=[("active", ACCENT_DK), ("disabled", "#c7cbe8")])

    # ---- helpers ----------------------------------------------------------

    def _browse(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
        if chosen:
            self.out_var.set(chosen)

    def _on_wheel(self, event) -> None:
        if getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.canvas.yview_scroll(1, "units")
        elif getattr(event, "delta", 0):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _logln(self, text: str) -> None:
        self._log_q.put(text)

    def _drain_log(self) -> None:
        try:
            while True:
                line = self._log_q.get_nowait()
                self.log["state"] = "normal"
                self.log.insert("end", line + "\n")
                self.log.see("end")
                self.log["state"] = "disabled"
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _build_config(self) -> Config | None:
        try:
            workers = int(self.workers_var.get())
            delay = float(self.delay_var.get())
        except (ValueError, self.tk.TclError):
            self.status_var.set("Workers/delay must be numbers.")
            return None
        return self.config.with_overrides(
            output_dir=Path(self.out_var.get() or "downloads"),
            max_workers=workers,
            request_delay=delay,
            full_size=bool(self.fullsize_var.get()),
        )

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.search_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")
        has = bool(self.galleries)
        self.download_all_btn.configure(
            state="disabled" if running or not has else "normal")
        for card in self._cards:
            card["btn"].configure(state="disabled" if running else "normal")

    def _run_bg(self, target, busy_status: str) -> None:
        if self._busy():
            return
        cfg = self._build_config()
        if cfg is None:
            return
        cancel = threading.Event()
        self.cancel_event = cancel

        handler = _QueueLogHandler(self._log_q)
        handler.setFormatter(logging.Formatter("%(message)s"))
        pkg_logger = logging.getLogger("bbjp_downloader")
        pkg_logger.setLevel(logging.INFO)
        pkg_logger.addHandler(handler)

        self._set_running(True)
        self.status_var.set(busy_status)

        def job() -> None:
            try:
                target(cfg, cancel)
            except Exception as exc:
                self._logln(f"Error: {exc}")
                self.root.after(0, lambda: self.status_var.set("Error — see log."))
            finally:
                pkg_logger.removeHandler(handler)
                self.root.after(0, lambda: self._set_running(False))

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    # ---- actions ----------------------------------------------------------

    def search(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            self.status_var.set("Please enter a name or URL.")
            return
        self.person = name
        self._run_bg(lambda cfg, ev: self._do_search(name, cfg, ev),
                     f"Searching “{name}” …")

    def _do_search(self, name, cfg, cancel) -> None:
        self._logln(f"Searching galleries for “{name}” …")
        galleries = Scraper(cfg, session=self.session,
                            cancel_event=cancel).find_galleries(name)
        self.galleries = galleries
        self.root.after(0, lambda: self._populate(galleries, cancel))

    def download_all(self) -> None:
        if not self.galleries:
            return
        person = self.person
        self._run_bg(lambda cfg, ev: self._do_download_all(person, cfg, ev),
                     "Downloading all galleries …")

    def _do_download_all(self, person, cfg, cancel) -> None:
        # Reuse the galleries already found by Search — no need to re-scrape.
        downloader = Downloader(cfg, session=self.session, cancel_event=cancel)
        stats = downloader.download_all(self.galleries, person_label(person))
        verb = "Stopped" if cancel.is_set() else "Done"
        msg = (f"{verb}: {stats.downloaded} downloaded, {stats.skipped} skipped,"
               f" {stats.failed} failed ({stats.bytes_written/1_048_576:.1f} MB).")
        self._logln(msg)
        self.root.after(0, lambda: self.status_var.set(msg))

    def download_one(self, gallery, card) -> None:
        person = self.person
        self._run_bg(
            lambda cfg, ev: self._do_download_one(gallery, card, person, cfg, ev),
            f"Downloading “{gallery.title or gallery.slug}” …")

    def _do_download_one(self, gallery, card, person, cfg, cancel) -> None:
        root = cfg.output_dir / sanitize_filename(person_label(person), "model")
        root.mkdir(parents=True, exist_ok=True)
        self.root.after(0, lambda: card["status"].configure(text="downloading…"))
        stats = Downloader(cfg, session=self.session,
                           cancel_event=cancel).download_gallery(gallery, root)
        verb = "stopped" if cancel.is_set() else "saved"
        text = f"{verb} · {stats.downloaded} new, {stats.skipped} skipped"
        self.root.after(0, lambda: card["status"].configure(text=text))
        self.root.after(0, lambda: self.status_var.set(
            f"“{gallery.title or gallery.slug}” — {text}."))

    def stop(self) -> None:
        if self.cancel_event and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.status_var.set("Stopping — finishing current file …")
            self._logln("Stop requested — files already saved are kept.")
        self.stop_btn.configure(state="disabled")

    def _on_close(self) -> None:
        if self.cancel_event:
            self.cancel_event.set()
        self.root.destroy()

    # ---- results rendering ------------------------------------------------

    def _populate(self, galleries, cancel) -> None:
        for child in self.results.winfo_children():
            child.destroy()
        self._cards = []
        self._thumb_refs = []

        if cancel.is_set():
            self.status_var.set("Search stopped.")
        if not galleries:
            self._empty = self.tk.Label(
                self.results, bg=BG, fg=MUTED, font=self.f_body,
                text="\n\nNo galleries found for that name.")
            self._empty.pack(pady=40)
            self.status_var.set("No galleries found.")
            self.download_all_btn.configure(state="disabled")
            return

        total_imgs = sum(len(g.images) for g in galleries)
        self.status_var.set(
            f"Found {len(galleries)} gallery(ies), {total_imgs} image(s).")
        self.download_all_btn.configure(state="normal")

        for gallery in galleries:
            self._add_card(gallery)
            if gallery.images:
                self._spawn_thumb(gallery, self._cards[-1])

    def _add_card(self, gallery) -> None:
        tk, ttk = self.tk, self.ttk
        card = tk.Frame(self.results, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=4, pady=5)

        thumb_holder = tk.Frame(card, bg="#eceef1", width=THUMB_W, height=THUMB_H)
        thumb_holder.pack(side="left", padx=10, pady=10)
        thumb_holder.pack_propagate(False)
        thumb = tk.Label(thumb_holder, bg="#eceef1", fg=MUTED,
                         text="🖼" if _HAVE_PIL else "no\npreview",
                         font=self.f_small)
        thumb.pack(fill="both", expand=True)

        right = tk.Frame(card, bg=CARD)
        right.pack(side="left", fill="both", expand=True, padx=(4, 10), pady=10)

        title = gallery.title or gallery.slug
        tk.Label(right, text=title, bg=CARD, fg=INK, font=self.f_bold,
                 anchor="w", justify="left", wraplength=440).pack(
                     anchor="w", fill="x")
        tk.Label(right, text=f"{len(gallery.images)} images", bg=CARD, fg=MUTED,
                 font=self.f_small, anchor="w").pack(anchor="w", pady=(2, 0))
        tk.Label(right, text=gallery.url, bg=CARD, fg=MUTED, font=self.f_small,
                 anchor="w", justify="left", wraplength=440).pack(
                     anchor="w", pady=(1, 6))

        controls = tk.Frame(right, bg=CARD)
        controls.pack(anchor="w", fill="x")
        entry = {"gallery": gallery, "thumb": thumb, "status": None, "btn": None}
        btn = ttk.Button(controls, text="Download", style="Card.TButton",
                         command=lambda g=gallery: self.download_one(g, entry))
        btn.pack(side="left")
        status = tk.Label(controls, text="", bg=CARD, fg=MUTED,
                          font=self.f_small)
        status.pack(side="left", padx=(8, 0))
        entry["btn"] = btn
        entry["status"] = status
        self._cards.append(entry)

    # ---- thumbnails -------------------------------------------------------

    def _spawn_thumb(self, gallery, card) -> None:
        if not _HAVE_PIL:
            return
        url = gallery.images[0]
        threading.Thread(target=self._load_thumb, args=(url, card),
                         daemon=True).start()

    def _load_thumb(self, url, card) -> None:
        with self._thumb_sema:
            try:
                resp = self.thumb_session.get(
                    url, timeout=self.config.timeout, stream=True)
                resp.raise_for_status()
                data = resp.content
                resp.close()
                image = Image.open(io.BytesIO(data))
                image.load()                       # decode off the UI thread
                image.thumbnail((THUMB_W, THUMB_H))
            except Exception:
                return

        # PhotoImage touches the Tk interpreter, so build it on the main thread.
        def apply() -> None:
            if not card["thumb"].winfo_exists():
                return
            try:
                photo = ImageTk.PhotoImage(image)
            except Exception:
                return
            self._thumb_refs.append(photo)
            card["thumb"].configure(image=photo, text="")
            card["thumb"].image = photo
        self.root.after(0, apply)


if __name__ == "__main__":
    raise SystemExit(launch())
