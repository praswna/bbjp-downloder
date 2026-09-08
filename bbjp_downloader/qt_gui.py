"""A modern PySide6/Qt GUI.

Search shows a card per gallery immediately (title + featured thumbnail from
the listing page) laid out in a responsive grid that reflows on resize; each
gallery's image count fills in afterwards on a background thread. Every card
has its own Download button plus a "Link ↗" button to open the gallery page in
the system browser. The toolbar has Download-all, Open-folder, Batch download
(search + download every gallery for a whole list of names, one after
another), a Settings dialog (workers / delay / full-size / save location /
browser mode) and Copy/Clear log.

Importing this module requires PySide6; the caller (cli) falls back to the
Tkinter GUI if that import fails. Thumbnails also use Pillow when available,
but Qt can decode JPEG/PNG itself so they work without it too.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import requests
from PySide6 import QtCore, QtGui, QtWidgets

from . import run
from .config import Config
from .downloader import Downloader, DownloadStats, sanitize_filename
from .scraper import Scraper, person_label

THUMB_W, THUMB_H = 200, 248
CARD_W = 236              # fixed tile width in the results grid
GRID_SPACING = 14
GRID_MAX_COLS = 6

_QSS = """
QWidget { font-family: "Segoe UI"; font-size: 13px; color: #203b36; }
QWidget#root { background: #f5f4ef; }
QWidget#masthead { background: #203b36; }
QLabel#header { background: transparent; color: #faf9f3; padding: 0; }
QLabel#headerSub { background: transparent; color: #b9cdc2; padding: 0; font-size: 11px; }
QLineEdit, QSpinBox, QDoubleSpinBox {
    border: 1px solid #dedfd6; border-radius: 9px; padding: 9px 11px;
    background: white; selection-background-color: #d5e9e1;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #176b60; }
QPushButton#accent {
    background: #176b60; color: white; border: none; border-radius: 9px;
    padding: 9px 18px; font-weight: 600;
}
QPushButton#accent:hover { background: #105348; }
QPushButton#accent:disabled { background: #dce5df; color: #65766d; }
QPushButton#danger {
    background: #ad4c45; color: white; border: none; border-radius: 9px;
    padding: 9px 18px; font-weight: 600;
}
QPushButton#danger:hover { background: #903e38; }
QPushButton#danger:disabled { background: #eaded9; color: #7e6c66; }
QPushButton#ghost {
    background: white; color: #203b36; border: 1px solid #dedfd6;
    border-radius: 9px; padding: 8px 13px;
}
QPushButton#ghost:hover { background: #eeeee7; }
QPushButton#card {
    background: #176b60; color: white; border: none; border-radius: 8px;
    padding: 7px 14px; font-weight: 600;
}
QPushButton#card:hover { background: #105348; }
QPushButton#card:disabled { background: #dce5df; color: #65766d; }
QPushButton#cardGhost {
    background: transparent; color: #176b60; border: 1px solid #dedfd6;
    border-radius: 8px; padding: 7px 10px; font-size: 12px; font-weight: 600;
}
QPushButton#cardGhost:hover { background: #eeeee7; }
QFrame#card { background: white; border: 1px solid #dedfd6; border-radius: 13px; }
QLabel#title { color: #203b36; font-size: 15px; font-weight: 600; }
QLabel#count { color: #176b60; font-size: 11px; }
QLabel#url { color: #73817a; font-size: 11px; }
QLabel#cardStatus { color: #5e7169; font-size: 11px; }
QLabel#thumb { background: #f5f4ef; border-radius: 9px; color: #73817a; }
QLabel#toolCount { color: #203b36; font-size: 15px; font-weight: 600; }
QLabel#hint { color: #5e7169; font-size: 11px; }
QLabel#status { color: #5e7169; font-size: 11px; }
QPlainTextEdit#log {
    background: #203b36; color: #d4e2da; border: none; border-radius: 9px;
    padding: 8px;
}
QScrollArea { border: none; background: #f5f4ef; }
QScrollBar:vertical { background: #f5f4ef; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #c4cec5; border-radius: 6px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QFrame#searchPanel { background: white; border: 1px solid #dedfd6; border-radius: 14px; }
QLabel#eyebrow { color: #176b60; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
QLabel#intro { font-size: 24px; font-weight: 600; color: #203b36; }
QLabel#empty { background: #eeeee7; color: #5e7169; border: 1px dashed #c4cec5; border-radius: 14px; font-size: 14px; padding: 28px; }
QPushButton:focus { border: 2px solid #9fbcaa; }
QPushButton:pressed { padding-top: 11px; }
QDialog { background: #f5f4ef; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #73817a; border-radius: 4px; background: white; }
QCheckBox::indicator:checked { background: #176b60; border: 3px solid #a9c9b6; }

"""


class _Signals(QtCore.QObject):
    log = QtCore.Signal(str)
    status = QtCore.Signal(str)
    stubs_ready = QtCore.Signal(object)          # list[GalleryStub]
    candidates_ready = QtCore.Signal(str, object)  # name, list[(label, url)]
    count_ready = QtCore.Signal(int, object)     # card index, Gallery|None
    thumb_ready = QtCore.Signal(int, bytes)      # card index, image bytes
    card_status = QtCore.Signal(int, str)        # card index, text
    finished = QtCore.Signal()


class _LogHandler(logging.Handler):
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        try:
            self.signal.emit(self.format(record))
        except RuntimeError:
            pass  # window gone


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.sig = _Signals()

        self._workers = config.max_workers
        self._delay = config.request_delay
        self._fullsize = config.full_size
        self._outdir = str(Path(config.output_dir).resolve())
        self._use_browser = True          # browser (Selenium) engine on by default
        self._headless = config.browser_headless
        self._browser = None              # persistent BrowserScraper

        self.cancel_event: threading.Event | None = None
        self.enrich_cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.enrich_thread: threading.Thread | None = None
        self.cards: list[dict] = []       # gallery tiles (empty in person-pick mode)
        self._tiles: list = []            # every frame currently in the grid,
                                          # gallery or person — drives layout
        self._last_cols = 0
        self.person = ""
        self._thumb_sema = threading.BoundedSemaphore(4)

        self.thumb_session = requests.Session()
        self.thumb_session.headers.update({
            "User-Agent": config.user_agent, "Referer": config.base_url})

        self._build()
        self._connect()

        handler = _LogHandler(self.sig.log)
        handler.setFormatter(logging.Formatter("%(message)s"))
        pkg = logging.getLogger("bbjp_downloader")
        pkg.setLevel(logging.INFO)
        pkg.addHandler(handler)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh_stop)
        self._timer.start(200)

    # ---- UI ---------------------------------------------------------------

    def _build(self) -> None:
        self.setWindowTitle("BBJP Gallery Downloader")
        self.resize(980, 820)
        self.setMinimumSize(760, 680)
        self.setStyleSheet(_QSS)

        root = QtWidgets.QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header
        header = QtWidgets.QWidget(objectName="masthead")
        hb = QtWidgets.QHBoxLayout(header)
        hb.setContentsMargins(28, 22, 28, 22)
        hb.setSpacing(0)
        h1 = QtWidgets.QLabel("BBJP  /  Gallery Archive", objectName="header")
        f = h1.font(); f.setPointSize(16); f.setBold(True); h1.setFont(f)
        sub = QtWidgets.QLabel("personal use · be gentle on the site",
                               objectName="headerSub")
        sub.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        hb.addWidget(h1); hb.addWidget(sub)
        outer.addWidget(header)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(28, 24, 28, 18)
        body.setSpacing(14)
        outer.addLayout(body)

        body.addWidget(QtWidgets.QLabel("YOUR PERSONAL COLLECTION", objectName="eyebrow"))
        body.addWidget(QtWidgets.QLabel("Find a gallery. Keep it organized.", objectName="intro"))

        # Search panel
        search_panel = QtWidgets.QFrame(objectName="searchPanel")
        search_layout = QtWidgets.QVBoxLayout(search_panel)
        search_layout.setContentsMargins(18, 16, 18, 16)
        search_layout.setSpacing(10)
        search_label = QtWidgets.QLabel("Name or gallery URL", objectName="toolCount")
        search_layout.addWidget(search_label)
        srow = QtWidgets.QHBoxLayout()
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText(
            "Model name, or paste a …/category/<name>/ URL")
        self.name_edit.returnPressed.connect(self.search)
        self.search_btn = QtWidgets.QPushButton("Search", objectName="accent")
        self.search_btn.clicked.connect(self.search)
        self.stop_btn = QtWidgets.QPushButton("Stop", objectName="danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        srow.addWidget(self.name_edit, 1)
        srow.addWidget(self.search_btn)
        srow.addWidget(self.stop_btn)
        search_label.setBuddy(self.name_edit)
        self.name_edit.setAccessibleName("Name or gallery URL")
        search_layout.addLayout(srow)
        search_layout.addWidget(QtWidgets.QLabel("Search by name, or paste a category / tag URL to get started.", objectName="hint"))
        body.addWidget(search_panel)

        # Toolbar row
        trow = QtWidgets.QHBoxLayout()
        self.tool_count = QtWidgets.QLabel("Galleries", objectName="toolCount")
        trow.addWidget(self.tool_count)
        trow.addStretch(1)
        for text, obj, slot in (
            ("Open folder", "ghost", self.open_folder),
            ("Batch download", "ghost", self.open_batch_dialog),
            ("Settings", "ghost", self.open_settings),
        ):
            b = QtWidgets.QPushButton(text, objectName=obj)
            b.clicked.connect(slot)
            trow.addWidget(b)
        self.dl_all_btn = QtWidgets.QPushButton("↓  Download all",
                                                objectName="accent")
        self.dl_all_btn.setEnabled(False)
        self.dl_all_btn.clicked.connect(self.download_all)
        trow.addWidget(self.dl_all_btn)
        body.addLayout(trow)

        # Results scroll area — a responsive grid of gallery tiles.
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_host = QtWidgets.QWidget(objectName="root")
        host_layout = QtWidgets.QVBoxLayout(self.list_host)
        host_layout.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)
        host_layout.setContentsMargins(0, 0, 6, 0)
        host_layout.setSpacing(12)

        self.empty = QtWidgets.QLabel(
            "Your collection starts here\n\nSearch above to discover galleries.\nThumbnails and image counts will appear here.")
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setObjectName("empty")
        self.empty.setMinimumHeight(180)
        host_layout.addWidget(self.empty)

        self.grid_widget = QtWidgets.QWidget(objectName="root")
        self.grid_layout = QtWidgets.QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(GRID_SPACING)
        self.grid_layout.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        host_layout.addWidget(self.grid_widget)
        host_layout.addStretch(1)

        self.scroll.setWidget(self.list_host)
        self.scroll.viewport().installEventFilter(self)
        body.addWidget(self.scroll, 1)

        # Log
        lrow = QtWidgets.QHBoxLayout()
        lrow.addWidget(QtWidgets.QLabel("ACTIVITY LOG", objectName="eyebrow"))
        lrow.addStretch(1)
        copy_b = QtWidgets.QPushButton("Copy", objectName="ghost")
        copy_b.clicked.connect(self.copy_log)
        clear_b = QtWidgets.QPushButton("Clear", objectName="ghost")
        clear_b.clicked.connect(self.clear_log)
        lrow.addWidget(copy_b); lrow.addWidget(clear_b)
        body.addLayout(lrow)

        self.log = QtWidgets.QPlainTextEdit(objectName="log")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(90)
        self.log.setFont(QtGui.QFont("Consolas", 10))
        body.addWidget(self.log)

        self.status = QtWidgets.QLabel("Ready.", objectName="status")
        body.addWidget(self.status)

        self._logln("Ready. For personal, lawful use only — respect the "
                    "site's terms, robots.txt and copyright.")

    def _connect(self) -> None:
        self.sig.log.connect(self._on_log)
        self.sig.status.connect(self.status.setText)
        self.sig.stubs_ready.connect(self._on_stubs)
        self.sig.candidates_ready.connect(self._on_candidates)
        self.sig.count_ready.connect(self._on_count)
        self.sig.thumb_ready.connect(self._on_thumb)
        self.sig.card_status.connect(self._on_card_status)
        self.sig.finished.connect(lambda: self._set_running(False))

    def eventFilter(self, obj, event) -> bool:
        if obj is self.scroll.viewport() and event.type() == QtCore.QEvent.Resize:
            self._relayout_grid()
        return super().eventFilter(obj, event)

    # ---- log --------------------------------------------------------------

    @QtCore.Slot(str)
    def _on_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _logln(self, text: str) -> None:
        self.log.appendPlainText(text)

    def clear_log(self) -> None:
        self.log.clear()

    def copy_log(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.log.toPlainText())
        self.status.setText("Log copied to clipboard.")

    # ---- misc actions -----------------------------------------------------

    def open_folder(self) -> None:
        path = Path(self._outdir or "downloads")
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            self.status.setText(f"Opened {path}")
        except Exception as exc:
            self.status.setText(f"Couldn't open folder: {exc}")

    def open_settings(self) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Settings")
        dlg.setStyleSheet(_QSS)
        form = QtWidgets.QFormLayout(dlg)
        form.setContentsMargins(20, 18, 20, 18)
        form.setSpacing(10)

        out_edit = QtWidgets.QLineEdit(self._outdir)
        out_edit.setMinimumWidth(320)
        browse = QtWidgets.QPushButton("Browse…", objectName="ghost")

        def do_browse():
            chosen = QtWidgets.QFileDialog.getExistingDirectory(
                dlg, "Choose save folder", out_edit.text() or ".")
            if chosen:
                out_edit.setText(chosen)
        browse.clicked.connect(do_browse)
        out_row = QtWidgets.QHBoxLayout()
        out_row.addWidget(out_edit, 1); out_row.addWidget(browse)
        form.addRow("Save location", out_row)

        workers = QtWidgets.QSpinBox(); workers.setRange(1, 16)
        workers.setValue(self._workers)
        form.addRow("Workers", workers)

        delay = QtWidgets.QDoubleSpinBox()
        delay.setRange(0, 10); delay.setSingleStep(0.5); delay.setValue(self._delay)
        form.addRow("Delay (s)", delay)

        full = QtWidgets.QCheckBox("Download full-size originals")
        full.setChecked(self._fullsize)
        form.addRow("", full)

        from .browser import selenium_available
        sel_ok = selenium_available()
        browser_chk = QtWidgets.QCheckBox(
            "Browser mode (Chrome via Selenium) — bypasses site blocks")
        browser_chk.setChecked(self._use_browser and sel_ok)
        browser_chk.setEnabled(sel_ok)
        if not sel_ok:
            browser_chk.setText(browser_chk.text()
                                + "  [needs: pip install selenium]")
        form.addRow("", browser_chk)

        headless_chk = QtWidgets.QCheckBox("Run browser headless (hidden window)")
        headless_chk.setChecked(self._headless)
        headless_chk.setEnabled(sel_ok)
        form.addRow("", headless_chk)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setObjectName("accent")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setObjectName("ghost")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)

        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self._outdir = out_edit.text().strip() or "downloads"
            self._workers = workers.value()
            self._delay = delay.value()
            self._fullsize = full.isChecked()
            new_use_browser = browser_chk.isChecked()
            new_headless = headless_chk.isChecked()
            if new_use_browser != self._use_browser or new_headless != self._headless:
                self._close_browser()  # settings changed — start fresh next run
            self._use_browser = new_use_browser
            self._headless = new_headless
            self.status.setText("Settings saved.")

    # ---- config / running state ------------------------------------------

    def _build_config(self) -> Config:
        return self.config.with_overrides(
            output_dir=Path(self._outdir or "downloads"),
            max_workers=int(self._workers),
            request_delay=float(self._delay),
            full_size=bool(self._fullsize),
            use_browser=bool(self._use_browser),
            browser_headless=bool(self._headless))

    def _scraper(self, cfg, cancel):
        """The scraper for this operation: a shared browser instance in browser
        mode (falling back to HTTP if Selenium isn't installed), else HTTP."""
        from .browser import selenium_available
        if self._use_browser and selenium_available():
            if self._browser is None:
                from .browser import BrowserScraper
                self._browser = BrowserScraper(cfg, cancel_event=cancel)
            self._browser.cancel_event = cancel
            return self._browser
        return Scraper(cfg, cancel_event=cancel)

    def _dl_session(self, cfg):
        """A cookie-seeded requests session when browsing, else None (the
        Downloader builds its own)."""
        if self._browser is not None:
            from .browser import download_session
            return download_session(cfg, self._browser.cookies())
        return None

    def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _set_running(self, running: bool) -> None:
        self.search_btn.setEnabled(not running)
        self.dl_all_btn.setEnabled(not running and bool(self.cards))
        for card in self.cards:
            card["btn"].setEnabled(not running)

    def _refresh_stop(self) -> None:
        active = self._busy() or (self.enrich_thread is not None
                                  and self.enrich_thread.is_alive())
        self.stop_btn.setEnabled(active)

    def _run_bg(self, target, busy_status: str) -> None:
        if self._busy():
            return
        cfg = self._build_config()
        self.cancel_event = threading.Event()
        self._set_running(True)
        self.sig.status.emit(busy_status)

        def job():
            try:
                target(cfg, self.cancel_event)
            except Exception as exc:
                self.sig.log.emit(f"Error: {exc}")
                self.sig.status.emit("Error — see log.")
            finally:
                self.sig.finished.emit()
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    # ---- search -----------------------------------------------------------

    def search(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.status.setText("Please enter a name or URL.")
            return
        self._search(name)

    def _search(self, name: str) -> None:
        """Run a search for ``name`` (a typed name or a gallery URL)."""
        self.person = name
        self.clear_log()
        self.enrich_cancel.set()
        self.enrich_cancel = threading.Event()
        self._run_bg(lambda cfg, ev: self._do_search(name, cfg, ev),
                     f"Searching “{name}” …")

    def _do_search(self, name, cfg, cancel) -> None:
        from .browser import selenium_available
        if self._use_browser and not selenium_available():
            self.sig.log.emit("Selenium not installed — using plain HTTP. "
                              "For browser mode: pip install selenium")
        elif self._use_browser:
            self.sig.log.emit("Browser mode: launching Chrome to read pages…")
        scraper = self._scraper(cfg, cancel)

        candidates = scraper.discover_candidates(name)
        if candidates:
            self.sig.log.emit(
                f"“{name}” matches {len(candidates)} different people — "
                f"pick one below.")
            self.sig.candidates_ready.emit(name, candidates)
            return

        self.sig.log.emit(f"Searching galleries for “{name}” …")
        stubs = scraper.find_gallery_stubs(name)
        self.sig.stubs_ready.emit(stubs)

    @QtCore.Slot(str, object)
    def _on_candidates(self, name, candidates) -> None:
        # Show the matching people right in the results grid — click one to
        # search just them — instead of interrupting with a separate dialog.
        self._clear_grid()
        self.empty.hide()
        self.tool_count.setText(f"{len(candidates)} people match “{name}”")
        self.status.setText(
            f"“{name}” matches {len(candidates)} people — pick one below.")
        self.dl_all_btn.setEnabled(False)
        for label, url in candidates:
            self._add_person_tile(label, url)
        self._relayout_grid(force=True)

    def _pick_person(self, url: str) -> None:
        self.name_edit.setText(url)
        self._search(url)

    def _add_person_tile(self, label: str, url: str) -> None:
        frame = QtWidgets.QFrame(objectName="card")
        frame.setFixedWidth(CARD_W)
        v = QtWidgets.QVBoxLayout(frame)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        icon = QtWidgets.QLabel("👤", objectName="thumb")
        icon.setFixedSize(THUMB_W, THUMB_H)
        icon.setAlignment(QtCore.Qt.AlignCenter)
        icon_font = icon.font()
        icon_font.setPointSize(40)
        icon.setFont(icon_font)
        icon_row = QtWidgets.QHBoxLayout()
        icon_row.addStretch(1)
        icon_row.addWidget(icon)
        icon_row.addStretch(1)
        v.addLayout(icon_row)

        title = QtWidgets.QLabel(label or url, objectName="title")
        title.setWordWrap(True)
        title.setAlignment(QtCore.Qt.AlignCenter)
        v.addWidget(title)

        btn = QtWidgets.QPushButton("View galleries →", objectName="card")
        btn.setToolTip(url)
        btn.clicked.connect(lambda _=False, u=url: self._pick_person(u))
        v.addWidget(btn)

        self._tiles.append(frame)

    @QtCore.Slot(object)
    def _on_stubs(self, stubs) -> None:
        self._clear_grid()
        if not stubs:
            self.empty.setText("No galleries found for that name.")
            self.empty.show()
            self.tool_count.setText("")
            self.dl_all_btn.setEnabled(False)
            self.status.setText("No galleries found.")
            return
        self.empty.hide()
        self.tool_count.setText(f"{len(stubs)} galleries")
        self.status.setText(f"Found {len(stubs)} galleries. Loading thumbnails…")
        self.dl_all_btn.setEnabled(True)
        for stub in stubs:
            self._add_card(stub)
            if stub.thumb:
                self._load_thumb(len(self.cards) - 1, stub.thumb)
        self._relayout_grid(force=True)
        self._start_enrichment()

    def _clear_grid(self) -> None:
        """Remove every tile currently shown — gallery cards or person tiles."""
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.cards = []
        self._tiles = []
        self._last_cols = 0

    def _relayout_grid(self, force: bool = False) -> None:
        """Re-flow the current tiles into a grid whose column count fits the
        viewport width — called on add and on window resize."""
        if not self._tiles:
            return
        viewport_w = self.scroll.viewport().width()
        cols = max(1, (viewport_w + GRID_SPACING) // (CARD_W + GRID_SPACING))
        cols = min(int(cols), GRID_MAX_COLS)
        if not force and cols == self._last_cols:
            return
        self._last_cols = cols
        for frame in self._tiles:
            self.grid_layout.removeWidget(frame)
        for i, frame in enumerate(self._tiles):
            row, col = divmod(i, cols)
            self.grid_layout.addWidget(frame, row, col)

    @staticmethod
    def _short_url(url: str, maxlen: int = 34) -> str:
        if len(url) <= maxlen:
            return url
        head = maxlen // 2 - 2
        tail = maxlen - head - 3
        return url[:head] + "..." + url[-tail:]

    def _open_link(self, url: str) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        self.status.setText(f"Opened in your browser: {url}")

    def _add_card(self, stub) -> None:
        # NOTE: the tile is only built here — its position in the results
        # grid is assigned by _relayout_grid(), called after all cards for a
        # search are added (and again on window resize).
        idx = len(self.cards)
        frame = QtWidgets.QFrame(objectName="card")
        frame.setFixedWidth(CARD_W)
        v = QtWidgets.QVBoxLayout(frame)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(8)

        thumb = QtWidgets.QLabel("PREVIEW", objectName="thumb")
        thumb.setFixedSize(THUMB_W, THUMB_H)
        thumb.setAlignment(QtCore.Qt.AlignCenter)
        thumb_row = QtWidgets.QHBoxLayout()
        thumb_row.addStretch(1)
        thumb_row.addWidget(thumb)
        thumb_row.addStretch(1)
        v.addLayout(thumb_row)

        title = QtWidgets.QLabel(stub.title, objectName="title")
        title.setWordWrap(True)
        title.setMaximumHeight(56)
        v.addWidget(title)

        count = QtWidgets.QLabel("counting…", objectName="count")
        v.addWidget(count)

        url = QtWidgets.QLabel(self._short_url(stub.url), objectName="url")
        url.setToolTip(stub.url)
        v.addWidget(url)

        ctl = QtWidgets.QHBoxLayout()
        ctl.setSpacing(8)
        btn = QtWidgets.QPushButton("↓ Download", objectName="card")
        btn.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                          QtWidgets.QSizePolicy.Fixed)
        btn.clicked.connect(lambda _=False, i=idx: self.download_one(i))
        link_btn = QtWidgets.QPushButton("Link ↗", objectName="cardGhost")
        link_btn.setToolTip("Open this gallery page in your browser")
        link_btn.clicked.connect(lambda _=False, u=stub.url: self._open_link(u))
        ctl.addWidget(btn, 1)
        ctl.addWidget(link_btn)
        v.addLayout(ctl)

        cstatus = QtWidgets.QLabel("", objectName="cardStatus")
        cstatus.setWordWrap(True)
        v.addWidget(cstatus)

        self.cards.append({"stub": stub, "gallery": None, "frame": frame,
                           "thumb": thumb, "count": count, "btn": btn,
                           "status": cstatus})
        self._tiles.append(frame)

    # ---- enrichment -------------------------------------------------------

    def _start_enrichment(self) -> None:
        if self.enrich_thread is not None and self.enrich_thread.is_alive():
            return
        cfg = self._build_config()
        cancel = self.enrich_cancel
        cards = list(self.cards)

        def work():
            scraper = self._scraper(cfg, cancel)
            for idx, card in enumerate(cards):
                if cancel.is_set():
                    return
                if card["gallery"] is not None:
                    continue
                try:
                    gallery = scraper.extract_images(card["stub"].url)
                except Exception:
                    gallery = None
                self.sig.count_ready.emit(idx, gallery)
            self.sig.status.emit(f"{len(cards)} galleries ready.")
        self.enrich_thread = threading.Thread(target=work, daemon=True)
        self.enrich_thread.start()

    @QtCore.Slot(int, object)
    def _on_count(self, idx, gallery) -> None:
        if idx >= len(self.cards):
            return
        card = self.cards[idx]
        if gallery is None or not gallery.images:
            card["count"].setText("no images")
            card["count"].setStyleSheet("color:#5e7169;")
            return
        card["gallery"] = gallery
        n = len(gallery.images)
        card["count"].setText(f"{n} image{'s' if n != 1 else ''}")
        card["count"].setStyleSheet("color:#5e7169;")
        if not card["stub"].thumb:
            self._load_thumb(idx, gallery.images[0])

    # ---- thumbnails -------------------------------------------------------

    def _load_thumb(self, idx, url) -> None:
        threading.Thread(target=self._thumb_worker, args=(idx, url),
                         daemon=True).start()

    def _thumb_worker(self, idx, url) -> None:
        with self._thumb_sema:
            if self.enrich_cancel.is_set():
                return
            try:
                resp = self.thumb_session.get(url, timeout=self.config.timeout,
                                              stream=True)
                resp.raise_for_status()
                data = resp.content
                resp.close()
            except Exception:
                return
        self.sig.thumb_ready.emit(idx, data)

    @QtCore.Slot(int, bytes)
    def _on_thumb(self, idx, data) -> None:
        if idx >= len(self.cards):
            return
        pix = QtGui.QPixmap()
        if not pix.loadFromData(data):
            return
        pix = pix.scaled(THUMB_W, THUMB_H, QtCore.Qt.KeepAspectRatioByExpanding,
                         QtCore.Qt.SmoothTransformation)
        self.cards[idx]["thumb"].setPixmap(pix)

    # ---- downloads --------------------------------------------------------

    def download_all(self) -> None:
        if not self.cards:
            return
        self.enrich_cancel.set()
        self._run_bg(self._do_download_all, "Downloading all galleries …")

    def _do_download_all(self, cfg, cancel) -> None:
        root = cfg.output_dir / sanitize_filename(person_label(self.person),
                                                  "model")
        root.mkdir(parents=True, exist_ok=True)
        scraper = self._scraper(cfg, cancel)
        downloader = Downloader(cfg, session=self._dl_session(cfg),
                                cancel_event=cancel)
        total = DownloadStats()
        cards = list(self.cards)
        for i, card in enumerate(cards):
            if cancel.is_set():
                break
            gallery = card["gallery"]
            if gallery is None:
                gallery = scraper.extract_images(card["stub"].url)
                if gallery:
                    self.sig.count_ready.emit(i, gallery)
            if not gallery or not gallery.images:
                continue
            self.sig.card_status.emit(i, "downloading…")
            st = downloader.download_gallery(gallery, root)
            total.merge(st)
            self.sig.card_status.emit(
                i, f"saved · {st.downloaded} new, {st.skipped} skipped")
            self.sig.status.emit(f"Downloading… {i + 1}/{len(cards)}")
        verb = "Stopped" if cancel.is_set() else "Done"
        self.sig.log.emit(
            f"{verb}: {total.downloaded} downloaded, {total.skipped} skipped, "
            f"{total.failed} failed ({total.bytes_written/1_048_576:.1f} MB).")
        self.sig.status.emit(verb + ".")

    def download_one(self, idx) -> None:
        if idx >= len(self.cards):
            return
        self.enrich_cancel.set()
        title = self.cards[idx]["stub"].title
        self._run_bg(lambda cfg, ev: self._do_download_one(idx, cfg, ev),
                     f"Downloading “{title}” …")

    def _do_download_one(self, idx, cfg, cancel) -> None:
        card = self.cards[idx]
        root = cfg.output_dir / sanitize_filename(person_label(self.person),
                                                  "model")
        root.mkdir(parents=True, exist_ok=True)
        gallery = card["gallery"]
        if gallery is None:
            gallery = self._scraper(cfg, cancel).extract_images(
                card["stub"].url)
            if gallery:
                self.sig.count_ready.emit(idx, gallery)
        if not gallery or not gallery.images:
            self.sig.card_status.emit(idx, "no images")
            return
        self.sig.card_status.emit(idx, "downloading…")
        st = Downloader(cfg, session=self._dl_session(cfg),
                        cancel_event=cancel).download_gallery(gallery, root)
        verb = "stopped" if cancel.is_set() else "saved"
        text = f"{verb} · {st.downloaded} new, {st.skipped} skipped"
        self.sig.card_status.emit(idx, text)
        self.sig.status.emit(f"“{card['stub'].title}” — {text}.")

    @QtCore.Slot(int, str)
    def _on_card_status(self, idx, text) -> None:
        if idx < len(self.cards):
            self.cards[idx]["status"].setText(text)

    # ---- batch download (multiple people in one run) ----------------------

    def open_batch_dialog(self) -> None:
        if self._busy():
            self.status.setText("Please wait for the current operation to finish.")
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Batch download — multiple people")
        dlg.setStyleSheet(_QSS)
        dlg.resize(480, 440)
        v = QtWidgets.QVBoxLayout(dlg)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)
        v.addWidget(QtWidgets.QLabel("One name or gallery URL per line",
                                     objectName="toolCount"))
        text = QtWidgets.QPlainTextEdit()
        text.setPlaceholderText(
            "Miura Sakura\nShinozaki Ai\n"
            "https://www.bigboobsjapan.com/category/.../\n…")
        text.setMinimumHeight(240)
        v.addWidget(text, 1)
        v.addWidget(QtWidgets.QLabel(
            "Each entry is searched and every gallery found for it is "
            "downloaded, one person after another, into its own folder. "
            "This can take a while — Stop cancels at any point.",
            objectName="hint"))
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QtWidgets.QDialogButtonBox.Ok)
        ok_btn.setText("Start batch download")
        ok_btn.setObjectName("accent")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setObjectName("ghost")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)

        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        names = [ln.strip() for ln in text.toPlainText().splitlines()
                if ln.strip()]
        if not names:
            self.status.setText("No names entered.")
            return
        self.batch_download(names)

    def batch_download(self, names: list[str]) -> None:
        if not names:
            return
        self.enrich_cancel.set()
        self._run_bg(lambda cfg, ev: self._do_batch(names, cfg, ev),
                     f"Batch: starting {len(names)} name(s) …")

    def _do_batch(self, names, cfg, cancel) -> None:
        total_people = len(names)
        grand = DownloadStats()
        for i, name in enumerate(names, 1):
            if cancel.is_set():
                break
            self.sig.log.emit(f"\n=== [{i}/{total_people}] {name} ===")
            self.sig.status.emit(f"Batch {i}/{total_people}: searching “{name}” …")
            scraper = self._scraper(cfg, cancel)
            stubs = scraper.find_gallery_stubs(name)
            self.sig.stubs_ready.emit(stubs)   # visual feedback only
            if cancel.is_set():
                break
            if not stubs:
                self.sig.log.emit(f"No galleries found for “{name}”.")
                continue
            root = cfg.output_dir / sanitize_filename(person_label(name),
                                                      "model")
            root.mkdir(parents=True, exist_ok=True)
            downloader = Downloader(cfg, session=self._dl_session(cfg),
                                    cancel_event=cancel)
            for j, stub in enumerate(stubs):
                if cancel.is_set():
                    break
                gallery = scraper.extract_images(stub.url)
                self.sig.count_ready.emit(j, gallery)
                if not gallery or not gallery.images:
                    continue
                self.sig.card_status.emit(j, "downloading…")
                st = downloader.download_gallery(gallery, root)
                grand.merge(st)
                self.sig.card_status.emit(
                    j, f"saved · {st.downloaded} new, {st.skipped} skipped")
                self.sig.status.emit(
                    f"Batch {i}/{total_people} “{name}”: "
                    f"{j + 1}/{len(stubs)} galleries")
            self.sig.log.emit(
                f"“{name}” done — {grand.downloaded} downloaded so far "
                f"(cumulative across this batch).")
        verb = "Stopped" if cancel.is_set() else "Done"
        self.sig.log.emit(
            f"\nBatch {verb}: {grand.downloaded} downloaded, {grand.skipped} "
            f"skipped, {grand.failed} failed "
            f"({grand.bytes_written / 1_048_576:.1f} MB) across "
            f"{total_people} name(s).")
        self.sig.status.emit(f"Batch {verb.lower()}.")

    # ---- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        if self.cancel_event and not self.cancel_event.is_set():
            self.cancel_event.set()
        self.enrich_cancel.set()
        self.status.setText("Stopping — finishing current file …")
        self._logln("Stop requested — files already saved are kept.")

    def closeEvent(self, event) -> None:
        if self.cancel_event:
            self.cancel_event.set()
        self.enrich_cancel.set()
        self._close_browser()
        super().closeEvent(event)


def launch(config: Config | None = None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow(config or Config())
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch())
