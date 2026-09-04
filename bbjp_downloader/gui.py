"""A small Tkinter GUI wrapper around the downloader.

Runs the scrape/download on a background thread and streams log output into a
text box so the window stays responsive. Tkinter ships with CPython, so this
needs no extra dependency beyond the scraper's own.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

from . import run
from .config import Config


class _QueueLogHandler(logging.Handler):
    """Push log records onto a thread-safe queue for the UI to drain."""

    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(self.format(record))


def launch(config: Config | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except Exception as exc:  # pragma: no cover - depends on the environment
        print(f"Tkinter is not available: {exc}")
        print("Use the command line instead, e.g.:  bbjp-downloader \"Name\"")
        return 1

    config = config or Config()
    log_queue: "queue.Queue[str]" = queue.Queue()

    root = tk.Tk()
    root.title("BBJP Gallery Downloader")
    root.geometry("640x480")
    root.minsize(560, 420)

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(1, weight=1)

    # --- Name ---
    ttk.Label(main, text="Name / URL:").grid(row=0, column=0, sticky="w", pady=4)
    name_var = tk.StringVar()
    name_entry = ttk.Entry(main, textvariable=name_var)
    name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
    name_entry.focus()

    # --- Output dir ---
    ttk.Label(main, text="Save to:").grid(row=1, column=0, sticky="w", pady=4)
    out_var = tk.StringVar(value=str(config.output_dir.resolve()))
    ttk.Entry(main, textvariable=out_var).grid(row=1, column=1, sticky="ew", pady=4)

    def browse():
        chosen = filedialog.askdirectory(initialdir=out_var.get() or ".")
        if chosen:
            out_var.set(chosen)

    ttk.Button(main, text="Browse…", command=browse).grid(
        row=1, column=2, sticky="e", padx=(6, 0))

    # --- Options ---
    opts = ttk.LabelFrame(main, text="Options", padding=8)
    opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)
    opts.columnconfigure(1, weight=1)
    opts.columnconfigure(3, weight=1)

    workers_var = tk.IntVar(value=config.max_workers)
    delay_var = tk.DoubleVar(value=config.request_delay)
    limit_var = tk.StringVar(value="")
    fullsize_var = tk.BooleanVar(value=config.full_size)

    ttk.Label(opts, text="Workers:").grid(row=0, column=0, sticky="w")
    ttk.Spinbox(opts, from_=1, to=16, width=5, textvariable=workers_var).grid(
        row=0, column=1, sticky="w", padx=4)
    ttk.Label(opts, text="Delay (s):").grid(row=0, column=2, sticky="w")
    ttk.Spinbox(opts, from_=0, to=10, increment=0.5, width=5,
                textvariable=delay_var).grid(row=0, column=3, sticky="w", padx=4)
    ttk.Label(opts, text="Max galleries:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(opts, width=7, textvariable=limit_var).grid(
        row=1, column=1, sticky="w", padx=4)
    ttk.Checkbutton(opts, text="Full-size originals",
                    variable=fullsize_var).grid(row=1, column=2, columnspan=2,
                                                sticky="w")

    # --- Log ---
    log = tk.Text(main, height=12, wrap="word", state="disabled")
    log.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(4, 8))
    main.rowconfigure(3, weight=1)
    scrollbar = ttk.Scrollbar(main, command=log.yview)
    scrollbar.grid(row=3, column=3, sticky="ns")
    log["yscrollcommand"] = scrollbar.set

    status_var = tk.StringVar(value="Ready.")
    ttk.Label(main, textvariable=status_var).grid(
        row=4, column=0, columnspan=2, sticky="w")

    worker: dict[str, threading.Thread | None] = {"t": None}

    def append(line: str) -> None:
        log["state"] = "normal"
        log.insert("end", line + "\n")
        log.see("end")
        log["state"] = "disabled"

    def drain_queue() -> None:
        try:
            while True:
                append(log_queue.get_nowait())
        except queue.Empty:
            pass
        root.after(120, drain_queue)

    def start() -> None:
        if worker["t"] and worker["t"].is_alive():
            return
        name = name_var.get().strip()
        if not name:
            status_var.set("Please enter a name.")
            return

        limit = None
        if limit_var.get().strip():
            try:
                limit = int(limit_var.get())
            except ValueError:
                status_var.set("Max galleries must be a whole number.")
                return

        run_config = config.with_overrides(
            output_dir=Path(out_var.get() or "downloads"),
            max_workers=int(workers_var.get()),
            request_delay=float(delay_var.get()),
            max_galleries=limit,
            full_size=bool(fullsize_var.get()),
        )

        handler = _QueueLogHandler(log_queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        pkg_logger = logging.getLogger("bbjp_downloader")
        pkg_logger.setLevel(logging.INFO)
        pkg_logger.addHandler(handler)

        start_btn["state"] = "disabled"
        status_var.set(f"Working on “{name}” …")

        def job() -> None:
            try:
                stats = run(name, run_config)
                log_queue.put(
                    f"\nDone: {stats.downloaded} downloaded, "
                    f"{stats.skipped} skipped, {stats.failed} failed "
                    f"({stats.bytes_written / 1_048_576:.1f} MB)."
                )
                root.after(0, lambda: status_var.set("Done."))
            except Exception as exc:  # surface any crash in the UI
                log_queue.put(f"\nError: {exc}")
                root.after(0, lambda: status_var.set("Error — see log."))
            finally:
                pkg_logger.removeHandler(handler)
                root.after(0, lambda: start_btn.configure(state="normal"))

        worker["t"] = threading.Thread(target=job, daemon=True)
        worker["t"].start()

    start_btn = ttk.Button(main, text="Download", command=start)
    start_btn.grid(row=4, column=2, sticky="e")
    name_entry.bind("<Return>", lambda _e: start())

    append(
        "Enter a name — or paste a gallery URL (e.g. a …/category/<name>/ "
        "page) — and press Download.\n"
        "For personal, lawful use only — please respect the site's terms, "
        "robots.txt and copyright.\n"
    )
    drain_queue()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch())
