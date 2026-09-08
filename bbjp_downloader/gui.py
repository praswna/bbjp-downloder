"""A modern Tkinter GUI: instant thumbnail cards, per-gallery download.

Search shows a card per gallery immediately (title + featured thumbnail taken
straight from the listing page); each gallery's image count fills in afterwards
on a background thread. Every card has its own Download button; the toolbar has
Download-all, Open-folder, Copy-log and a Settings dialog (workers / delay /
full-size / save location). All network work runs off the UI thread.

Thumbnails need Pillow. Without it the cards show a placeholder and the rest
still works.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import requests

from . import run
from .config import Config
from .downloader import Downloader, DownloadStats, sanitize_filename
from .scraper import Scraper, person_label

try:
    from PIL import Image, ImageTk
    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False


# --- palette --------------------------------------------------------------
BG = "#f5f4ef"        # window background
PANEL = "#ffffff"     # panels / cards
BORDER = "#dedfd6"    # hairline borders
INK = "#203b36"       # primary text
MUTED = "#5e7169"     # secondary text
FAINT = "#73817a"     # tertiary text
ACCENT = "#176b60"    # deep teal
ACCENT_DK = "#105348"
GHOST = "#f5f4ef"     # secondary button bg
GHOST_DK = "#dedfd6"
DANGER = "#ad4c45"
THUMB = "#f5f4ef"
THUMB_W, THUMB_H = 120, 150


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
    _App(tk, ttk, config or Config()).root.mainloop()
    return 0


class _App:
    def __init__(self, tk, ttk, config: Config):
        self.tk, self.ttk, self.config = tk, ttk, config

        self.cancel_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.enrich_cancel = threading.Event()
        self.enrich_thread: threading.Thread | None = None

        self.person = ""
        self._cards: list[dict] = []
        self._thumb_refs: list = []
        self._thumb_sema = threading.BoundedSemaphore(4)
        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._settings = None

        self.thumb_session = requests.Session()
        self.thumb_session.headers.update({
            "User-Agent": config.user_agent, "Referer": config.base_url})

        self._build()
        self._drain_log()

    # ---- construction -----------------------------------------------------

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = tk.Tk()
        self.root = root
        root.title("BBJP Gallery Downloader")
        root.geometry("920x760")
        root.minsize(680, 560)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._fonts()
        self._styles()

        # config-backed vars
        self.out_var = tk.StringVar(
            value=str(Path(self.config.output_dir).resolve()))
        self.workers_var = tk.IntVar(value=self.config.max_workers)
        self.delay_var = tk.DoubleVar(value=self.config.request_delay)
        self.fullsize_var = tk.BooleanVar(value=self.config.full_size)
        self.status_var = tk.StringVar(value="Ready.")

        # Header
        head = tk.Frame(root, bg=ACCENT, height=64)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="BBJP Gallery Downloader", bg=ACCENT, fg="white",
                 font=self.f_h1).pack(side="left", padx=20)
        tk.Label(head, text="personal use · be gentle on the site  ",
                 bg=ACCENT, fg="#d5e9e1", font=self.f_small).pack(
                     side="right", padx=8)

        # Search panel
        sp = tk.Frame(root, bg=BG)
        sp.pack(fill="x", padx=20, pady=(16, 6))
        card = tk.Frame(sp, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=12, pady=12)
        self.name_var = tk.StringVar()
        entry = ttk.Entry(inner, textvariable=self.name_var, font=self.f_body)
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        entry.bind("<Return>", lambda _e: self.search())
        entry.focus()
        self.search_btn = ttk.Button(inner, text="Search",
                                     style="Accent.TButton", command=self.search)
        self.search_btn.pack(side="left", padx=(10, 0))
        self.stop_btn = ttk.Button(inner, text="Stop", style="Danger.TButton",
                                   command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        tk.Label(sp, text="Type a model name, or paste a …/category/<name>/ URL.",
                 bg=BG, fg=MUTED, font=self.f_small).pack(anchor="w", pady=(6, 0))

        # Toolbar
        tb = tk.Frame(root, bg=BG)
        tb.pack(fill="x", padx=20, pady=(4, 4))
        self.count_lbl = tk.Label(tb, text="", bg=BG, fg=INK, font=self.f_bold)
        self.count_lbl.pack(side="left")
        self.dl_all_btn = ttk.Button(tb, text="⤓  Download all",
                                     style="Accent.TButton",
                                     command=self.download_all, state="disabled")
        self.dl_all_btn.pack(side="right")
        ttk.Button(tb, text="Settings", style="Ghost.TButton",
                   command=self._open_settings).pack(side="right", padx=(0, 8))
        ttk.Button(tb, text="Open folder", style="Ghost.TButton",
                   command=self._open_folder).pack(side="right", padx=(0, 8))

        # Results (scrollable)
        rc = tk.Frame(root, bg=BORDER)
        rc.pack(fill="both", expand=True, padx=20, pady=(4, 6))
        self.canvas = tk.Canvas(rc, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(rc, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.results = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.results,
                                              anchor="nw")
        self.results.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._win, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(seq, self._on_wheel)
        self._empty = tk.Label(
            self.results, bg=BG, fg=FAINT, font=self.f_body, justify="center",
            text="\n\nNothing yet — search for a name to see galleries.")
        self._empty.pack(pady=48)

        # Log
        lf = tk.Frame(root, bg=BG)
        lf.pack(fill="x", padx=20, pady=(0, 4))
        tk.Label(lf, text="Log", bg=BG, fg=MUTED, font=self.f_small).pack(
            side="left")
        ttk.Button(lf, text="Clear", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")
        ttk.Button(lf, text="Copy", style="Ghost.TButton",
                   command=self._copy_log).pack(side="right", padx=(0, 6))
        if not _HAVE_PIL:
            tk.Label(lf, text="install Pillow for thumbnails", bg=BG, fg=FAINT,
                     font=self.f_small).pack(side="left", padx=(10, 0))
        self.log = tk.Text(root, height=4, wrap="word", bg="#203b36",
                           fg="#d4e2da", bd=0, font=self.f_mono, state="disabled",
                           padx=10, pady=8)
        self.log.pack(fill="x", padx=20, pady=(0, 4))

        # Status
        tk.Label(root, textvariable=self.status_var, bg=BG, fg=MUTED,
                 font=self.f_small, anchor="w").pack(
                     fill="x", padx=22, pady=(0, 10))

        self._logln("Ready. For personal, lawful use only — respect the "
                    "site's terms, robots.txt and copyright.")

    def _fonts(self) -> None:
        import tkinter.font as tkfont
        fam = "Segoe UI"
        self.f_h1 = tkfont.Font(family=fam, size=16, weight="bold")
        self.f_body = tkfont.Font(family=fam, size=11)
        self.f_bold = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_title = tkfont.Font(family=fam, size=12, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_mono = tkfont.Font(family="Consolas", size=9)

    def _styles(self) -> None:
        st = self.ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TButton", font=self.f_body, padding=(12, 6), borderwidth=0)
        st.configure("Accent.TButton", foreground="white", background=ACCENT,
                     padding=(16, 7), borderwidth=0)
        st.map("Accent.TButton",
               background=[("active", ACCENT_DK), ("disabled", "#dce5df")])
        st.configure("Danger.TButton", foreground="white", background=DANGER,
                     padding=(16, 7), borderwidth=0)
        st.map("Danger.TButton",
               background=[("active", "#903e38"), ("disabled", "#eaded9")])
        st.configure("Ghost.TButton", foreground=INK, background=GHOST,
                     padding=(10, 5), borderwidth=0)
        st.map("Ghost.TButton",
               background=[("active", GHOST_DK), ("disabled", GHOST)])
        st.configure("Card.TButton", foreground="white", background=ACCENT,
                     padding=(12, 5), borderwidth=0)
        st.map("Card.TButton",
               background=[("active", ACCENT_DK), ("disabled", "#dce5df")])

    # ---- small helpers ----------------------------------------------------

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
        active = self._busy() or (self.enrich_thread is not None
                                  and self.enrich_thread.is_alive())
        self.stop_btn.configure(state="normal" if active else "disabled")
        self.root.after(120, self._drain_log)

    def _clear_log(self) -> None:
        self.log["state"] = "normal"
        self.log.delete("1.0", "end")
        self.log["state"] = "disabled"

    def _copy_log(self) -> None:
        text = self.log.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.status_var.set("Log copied to clipboard.")

    def _open_folder(self) -> None:
        path = Path(self.out_var.get() or "downloads")
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            self.status_var.set(f"Opened {path}")
        except Exception as exc:
            self.status_var.set(f"Couldn't open folder: {exc}")

    def _on_wheel(self, event) -> None:
        if getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.canvas.yview_scroll(1, "units")
        elif getattr(event, "delta", 0):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

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
            max_workers=workers, request_delay=delay,
            full_size=bool(self.fullsize_var.get()))

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.search_btn.configure(state=state)
        has = bool(self._cards)
        self.dl_all_btn.configure(
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
        pkg = logging.getLogger("bbjp_downloader")
        pkg.setLevel(logging.INFO)
        pkg.addHandler(handler)
        self._set_running(True)
        self.status_var.set(busy_status)

        def job() -> None:
            try:
                target(cfg, cancel)
            except Exception as exc:
                self._logln(f"Error: {exc}")
                self.root.after(0, lambda: self.status_var.set("Error — see log."))
            finally:
                pkg.removeHandler(handler)
                self.root.after(0, lambda: self._set_running(False))
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    # ---- settings dialog --------------------------------------------------

    def _open_settings(self) -> None:
        tk, ttk = self.tk, self.ttk
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.lift()
            return
        win = tk.Toplevel(self.root)
        self._settings = win
        win.title("Settings")
        win.configure(bg=PANEL)
        win.transient(self.root)
        win.resizable(False, False)
        frm = tk.Frame(win, bg=PANEL, padx=20, pady=18)
        frm.pack(fill="both", expand=True)

        tk.Label(frm, text="Save location", bg=PANEL, fg=INK,
                 font=self.f_bold).grid(row=0, column=0, columnspan=3,
                                        sticky="w")
        ttk.Entry(frm, textvariable=self.out_var, font=self.f_small,
                  width=42).grid(row=1, column=0, columnspan=2, sticky="we",
                                 pady=(2, 12))
        ttk.Button(frm, text="Browse…", style="Ghost.TButton",
                   command=self._browse).grid(row=1, column=2, padx=(8, 0))

        tk.Label(frm, text="Workers", bg=PANEL, fg=INK, font=self.f_bold).grid(
            row=2, column=0, sticky="w")
        ttk.Spinbox(frm, from_=1, to=16, width=6, textvariable=self.workers_var
                    ).grid(row=2, column=1, sticky="w", pady=4)
        tk.Label(frm, text="parallel image downloads (2–4 is plenty)", bg=PANEL,
                 fg=MUTED, font=self.f_small).grid(row=2, column=2, sticky="w")

        tk.Label(frm, text="Delay (s)", bg=PANEL, fg=INK, font=self.f_bold).grid(
            row=3, column=0, sticky="w")
        ttk.Spinbox(frm, from_=0, to=10, increment=0.5, width=6,
                    textvariable=self.delay_var).grid(row=3, column=1,
                                                      sticky="w", pady=4)
        tk.Label(frm, text="gap between requests — higher is safer", bg=PANEL,
                 fg=MUTED, font=self.f_small).grid(row=3, column=2, sticky="w")

        ttk.Checkbutton(frm, text="Download full-size originals",
                        variable=self.fullsize_var).grid(
                            row=4, column=0, columnspan=3, sticky="w", pady=(8, 12))

        ttk.Button(frm, text="Done", style="Accent.TButton",
                   command=win.destroy).grid(row=5, column=2, sticky="e")
        frm.columnconfigure(0, weight=1)

    def _browse(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
        if chosen:
            self.out_var.set(chosen)

    # ---- search -----------------------------------------------------------

    def search(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            self.status_var.set("Please enter a name or URL.")
            return
        self.person = name
        self._clear_log()                       # fresh log per search
        self.enrich_cancel.set()                # stop any previous enrichment
        self.enrich_cancel = threading.Event()
        self._run_bg(lambda cfg, ev: self._do_search(name, cfg, ev),
                     f"Searching “{name}” …")

    def _do_search(self, name, cfg, cancel) -> None:
        self._logln(f"Searching galleries for “{name}” …")
        stubs = Scraper(cfg, cancel_event=cancel).find_gallery_stubs(name)
        self.root.after(0, lambda: self._render(stubs, cancel))

    def _render(self, stubs, cancel) -> None:
        for child in self.results.winfo_children():
            child.destroy()
        self._cards = []
        self._thumb_refs = []

        if not stubs:
            msg = ("Search stopped." if cancel.is_set()
                   else "No galleries found for that name.")
            tk = self.tk
            tk.Label(self.results, bg=BG, fg=FAINT, font=self.f_body,
                     text="\n\n" + msg).pack(pady=48)
            self.count_lbl.configure(text="")
            self.dl_all_btn.configure(state="disabled")
            self.status_var.set(msg)
            return

        self.count_lbl.configure(text=f"{len(stubs)} galleries")
        self.status_var.set(
            f"Found {len(stubs)} galleries. Loading thumbnails…")
        self.dl_all_btn.configure(state="normal")
        for stub in stubs:
            card = self._add_card(stub)
            if stub.thumb:
                self._load_thumb(stub.thumb, card)
        self._start_enrichment()

    def _add_card(self, stub) -> dict:
        tk, ttk = self.tk, self.ttk
        card = tk.Frame(self.results, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=3, pady=5)

        holder = tk.Frame(card, bg=THUMB, width=THUMB_W, height=THUMB_H)
        holder.pack(side="left", padx=12, pady=12)
        holder.pack_propagate(False)
        thumb = tk.Label(holder, bg=THUMB, fg=FAINT,
                         text="🖼" if _HAVE_PIL else "no\npreview",
                         font=self.f_small)
        thumb.pack(fill="both", expand=True)

        right = tk.Frame(card, bg=PANEL)
        right.pack(side="left", fill="both", expand=True, padx=(2, 12), pady=12)
        tk.Label(right, text=stub.title, bg=PANEL, fg=INK, font=self.f_title,
                 anchor="w", justify="left", wraplength=520).pack(
                     anchor="w", fill="x")
        count_lbl = tk.Label(right, text="counting…", bg=PANEL, fg=ACCENT,
                             font=self.f_small, anchor="w")
        count_lbl.pack(anchor="w", pady=(3, 0))
        tk.Label(right, text=stub.url, bg=PANEL, fg=FAINT, font=self.f_small,
                 anchor="w", justify="left", wraplength=520).pack(
                     anchor="w", pady=(1, 8))

        ctl = tk.Frame(right, bg=PANEL)
        ctl.pack(anchor="w", fill="x")
        entry: dict = {"stub": stub, "thumb": thumb, "count_lbl": count_lbl,
                       "gallery": None, "btn": None, "status": None}
        btn = ttk.Button(ctl, text="⤓  Download", style="Card.TButton",
                         command=lambda c=entry: self.download_one(c))
        btn.pack(side="left")
        status = tk.Label(ctl, text="", bg=PANEL, fg=MUTED, font=self.f_small)
        status.pack(side="left", padx=(10, 0))
        entry["btn"], entry["status"] = btn, status
        self._cards.append(entry)
        return entry

    # ---- enrichment (image counts) ---------------------------------------

    def _start_enrichment(self) -> None:
        if self.enrich_thread is not None and self.enrich_thread.is_alive():
            return
        cfg = self._build_config()
        if cfg is None:
            return
        cancel = self.enrich_cancel
        cards = list(self._cards)

        def work() -> None:
            scraper = Scraper(cfg, cancel_event=cancel)
            done = 0
            for card in cards:
                if cancel.is_set():
                    return
                if card.get("gallery") is not None:
                    continue
                try:
                    gallery = scraper.extract_images(card["stub"].url)
                except Exception:
                    gallery = None
                self.root.after(0, lambda c=card, g=gallery: self._apply_count(c, g))
                done += 1
            self.root.after(0, lambda: self.status_var.set(
                f"{len(cards)} galleries ready."))
        self.enrich_thread = threading.Thread(target=work, daemon=True)
        self.enrich_thread.start()

    def _apply_count(self, card, gallery) -> None:
        if not card["count_lbl"].winfo_exists():
            return
        if gallery is None or not gallery.images:
            card["count_lbl"].configure(text="no images", fg=MUTED)
            return
        card["gallery"] = gallery
        n = len(gallery.images)
        card["count_lbl"].configure(text=f"{n} image{'s' if n != 1 else ''}",
                                    fg=MUTED)
        if not card["stub"].thumb:            # listing had no thumb — use first
            self._load_thumb(gallery.images[0], card)

    # ---- thumbnails -------------------------------------------------------

    def _load_thumb(self, url, card) -> None:
        if not _HAVE_PIL:
            return
        threading.Thread(target=self._thumb_worker, args=(url, card),
                         daemon=True).start()

    def _thumb_worker(self, url, card) -> None:
        with self._thumb_sema:
            if self.enrich_cancel.is_set():
                return
            try:
                resp = self.thumb_session.get(url, timeout=self.config.timeout,
                                              stream=True)
                resp.raise_for_status()
                data = resp.content
                resp.close()
                image = Image.open(io.BytesIO(data))
                image.load()
                image.thumbnail((THUMB_W, THUMB_H))
            except Exception:
                return

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

    # ---- downloads --------------------------------------------------------

    def download_all(self) -> None:
        if not self._cards:
            return
        self.enrich_cancel.set()              # downloads take over the network
        self._run_bg(self._do_download_all, "Downloading all galleries …")

    def _do_download_all(self, cfg, cancel) -> None:
        root = cfg.output_dir / sanitize_filename(person_label(self.person),
                                                  "model")
        root.mkdir(parents=True, exist_ok=True)
        downloader = Downloader(cfg, cancel_event=cancel)
        scraper = Scraper(cfg, cancel_event=cancel)
        total = DownloadStats()
        cards = list(self._cards)
        for i, card in enumerate(cards, 1):
            if cancel.is_set():
                break
            gallery = card.get("gallery")
            if gallery is None:
                gallery = scraper.extract_images(card["stub"].url)
                if gallery:
                    self.root.after(0, lambda c=card, g=gallery:
                                    self._apply_count(c, g))
            if not gallery or not gallery.images:
                continue
            self.root.after(0, lambda c=card: c["status"].configure(
                text="downloading…"))
            st = downloader.download_gallery(gallery, root)
            total.merge(st)
            self.root.after(0, lambda c=card, s=st: c["status"].configure(
                text=f"saved · {s.downloaded} new, {s.skipped} skipped"))
            self.root.after(0, lambda i=i, n=len(cards): self.status_var.set(
                f"Downloading… {i}/{n}"))
        verb = "Stopped" if cancel.is_set() else "Done"
        msg = (f"{verb}: {total.downloaded} downloaded, {total.skipped} skipped,"
               f" {total.failed} failed ({total.bytes_written/1_048_576:.1f} MB).")
        self._logln(msg)
        self.root.after(0, lambda: self.status_var.set(msg))

    def download_one(self, card) -> None:
        self.enrich_cancel.set()
        title = card["stub"].title
        self._run_bg(lambda cfg, ev: self._do_download_one(card, cfg, ev),
                     f"Downloading “{title}” …")

    def _do_download_one(self, card, cfg, cancel) -> None:
        root = cfg.output_dir / sanitize_filename(person_label(self.person),
                                                  "model")
        root.mkdir(parents=True, exist_ok=True)
        gallery = card.get("gallery")
        if gallery is None:
            gallery = Scraper(cfg, cancel_event=cancel).extract_images(
                card["stub"].url)
            if gallery:
                self.root.after(0, lambda: self._apply_count(card, gallery))
        if not gallery or not gallery.images:
            self.root.after(0, lambda: card["status"].configure(text="no images"))
            return
        self.root.after(0, lambda: card["status"].configure(text="downloading…"))
        st = Downloader(cfg, cancel_event=cancel).download_gallery(gallery, root)
        verb = "stopped" if cancel.is_set() else "saved"
        text = f"{verb} · {st.downloaded} new, {st.skipped} skipped"
        self.root.after(0, lambda: card["status"].configure(text=text))
        self.root.after(0, lambda: self.status_var.set(
            f"“{card['stub'].title}” — {text}."))

    # ---- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        if self.cancel_event and not self.cancel_event.is_set():
            self.cancel_event.set()
        self.enrich_cancel.set()
        self.status_var.set("Stopping — finishing current file …")
        self._logln("Stop requested — files already saved are kept.")

    def _on_close(self) -> None:
        if self.cancel_event:
            self.cancel_event.set()
        self.enrich_cancel.set()
        self.root.destroy()


if __name__ == "__main__":
    raise SystemExit(launch())
