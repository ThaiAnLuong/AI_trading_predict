"""Main window: paper-mode banner, tabs and status bar."""
from __future__ import annotations

import ctypes
import queue
import tkinter as tk
import traceback
from ctypes import wintypes
from tkinter import messagebox, ttk

from .. import __version__
from ..context import AppContext
from ..dataops import auto_backup, experiment_day
from ..service import Worker, log_update, needs_fx
from ..settings import save_settings
from ..timeutil import local_str
from ..valuation import latest_fx
from .admin import AboutTab, SettingsTab
from .common import DEMO_BANNER, FONT_BOLD, PAPER_BANNER, apply_theme
from .dashboard import AiAccountTab, MyAccountTab
from .lessons import LessonsTab
from .tabs import CompareTab, DecisionsTab, NewsTab, TradeLogTab, WatchlistTab

TIMER_MS = 60_000
POLL_MS = 250
DWMWA_USE_IMMERSIVE_DARK_MODE = 20


class App(tk.Tk):
    def __init__(self, ctx: AppContext):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        super().__init__()
        self.ctx = ctx
        self.events: queue.Queue = queue.Queue()
        self.state_text = "Starting..."
        self.last_ui_error = ""
        label = "PAPER MODE" + (" - DEMO, FAKE DATA" if ctx.demo else "")
        self.title(f"Paper Trader {__version__} - {label} - no real money")
        self.geometry("1320x860")
        self.minsize(1120, 720)
        self.dark = tk.BooleanVar(value=ctx.settings().dark_mode)
        apply_theme(self, self.dark.get())

        tk.Label(self, text="PAPER MODE: simulated trades only. No real money. No brokerage connected.",
                 background=PAPER_BANNER, foreground="white", font=FONT_BOLD, pady=5).pack(fill="x")
        if ctx.demo:
            tk.Label(self, text="DEMO MODE: all prices, news and history here are FAKE. "
                     "This data is stored separately from your experiment.",
                     background=DEMO_BANNER, foreground="white", font=FONT_BOLD, pady=5).pack(fill="x")

        bottom = ttk.Frame(self, padding=(10, 4))
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Exit app", command=self.exit_app).pack(side="right")
        ttk.Button(bottom, text="Refresh now", command=self.refresh_now).pack(side="right", padx=6)
        ttk.Checkbutton(bottom, text="Dark mode", variable=self.dark,
                        command=self.toggle_dark).pack(side="right", padx=6)
        self.status = ttk.Label(bottom, style="Muted.TLabel")
        self.status.pack(side="left")

        self.book = ttk.Notebook(self)
        self.book.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        self.tabs = []
        for title, cls in (("My account", MyAccountTab), ("AI account", AiAccountTab),
                           ("Compare", CompareTab), ("Watchlist", WatchlistTab),
                           ("Trade log", TradeLogTab), ("AI decisions", DecisionsTab),
                           ("AI lessons", LessonsTab), ("News", NewsTab), ("Settings", SettingsTab), ("About and data", AboutTab)):
            tab = cls(self.book, self)
            self.book.add(tab, text=title)
            self.tabs.append(tab)
        self.book.bind("<<NotebookTabChanged>>", lambda _e: self.refresh_visible())

        self.worker = Worker(ctx, lambda event: self.events.put(event))
        self.worker.start()
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)
        self.refresh_all()
        self.update_idletasks()
        self.set_title_bar()
        self.after(POLL_MS, self.poll)
        self.after(TIMER_MS, self.tick)
        self.after(3000, self.daily_backup)

    def report_callback_exception(self, exc, value, tb):
        detail = "".join(traceback.format_exception(exc, value, tb))
        log_update(self.ctx, "ui", "error", detail)
        messagebox.showerror("Paper Trader", f"Something went wrong:\n{value}", parent=self)

    def post(self, callback) -> None:
        """Run a callback on the UI thread (safe to call from any thread)."""
        self.events.put(callback)

    def poll(self) -> None:
        changed = False
        try:
            while True:
                event = self.events.get_nowait()
                if callable(event):
                    event()
                else:
                    changed = True
                    self.state_text = {"updating": "Updating data...", "updated": "Up to date"}.get(
                        event, f"Update problem: {event}")
        except queue.Empty:
            pass
        if changed:
            self.refresh_visible()
        self.after(POLL_MS, self.poll)

    def tick(self) -> None:
        self.refresh_visible()
        self.after(TIMER_MS, self.tick)

    def daily_backup(self) -> None:
        try:
            auto_backup(self.ctx)
        except (OSError, RuntimeError) as exc:
            log_update(self.ctx, "backup", "error", str(exc))

    def refresh_now(self) -> None:
        self.state_text = "Refresh requested..."
        self.worker.request_refresh()
        self.refresh_status()

    def refresh_tab(self, tab) -> None:
        """A display fault in one tab must not close the app or stop the experiment."""
        try:
            tab.refresh()
        except Exception as exc:
            detail = traceback.format_exc()
            if detail != self.last_ui_error:  # refreshes repeat, so log each fault once
                self.last_ui_error = detail
                log_update(self.ctx, "ui", "error", f"{type(tab).__name__}: {exc}\n{detail}")
            self.state_text = f"Display problem in {type(tab).__name__}: {exc}"

    def refresh_visible(self) -> None:
        self.refresh_tab(self.nametowidget(self.book.select()))
        self.refresh_status()

    def refresh_all(self) -> None:
        for tab in self.tabs:
            self.refresh_tab(tab)
        self.refresh_status()

    def tabs_loaded_reset(self) -> None:
        for tab in self.tabs:
            if isinstance(tab, SettingsTab):
                tab.loaded = False

    def refresh_status(self) -> None:
        conn = self.ctx.db.conn()
        s = self.ctx.settings()
        now = self.ctx.clock()
        market = self.ctx.market()
        last = conn.execute("SELECT MAX(fetched_ts) AS ts FROM quotes WHERE status = 'ok'").fetchone()["ts"]
        fx = latest_fx(conn, s, now)
        if needs_fx(self.ctx):
            fx_text = f"USD/CAD {fx.rate} ({fx.rate_date}, {fx.source})" if fx else "USD/CAD unavailable"
        else:
            fx_text = "CAD tickers only"
        ai = "off" if not s.ai_enabled else ("suggest only" if s.ai_suggest_only else "on")
        day, _ = experiment_day(self.ctx)
        self.status.configure(text=(
            f"PAPER MODE  |  Data: {market.name}  |  Prices updated: {local_str(last)}  |  {fx_text}  "
            f"|  AI: {ai}  |  Day {day}  |  {self.state_text}"))

    def set_title_bar(self) -> None:
        """Ask Windows to match the title bar to the theme. Cosmetic, so failure is ignored."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(int(self.dark.get()))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value),
                ctypes.sizeof(value))
        except (AttributeError, OSError):
            pass

    def toggle_dark(self) -> None:
        s = self.ctx.settings()
        s.dark_mode = self.dark.get()
        save_settings(self.ctx.db, s, self.ctx.clock)
        apply_theme(self, s.dark_mode)
        self.set_title_bar()

    def on_window_close(self) -> None:
        """The window's X only minimizes, so a stray click cannot stop the AI."""
        self.iconify()

    def exit_app(self) -> None:
        if messagebox.askyesno(
                "Exit Paper Trader",
                "Exit the app?\n\nWhile it is closed, AI trading, price updates and snapshots "
                "stop. Your data stays saved.", icon="warning", default="no", parent=self):
            self.close()

    def close(self) -> None:
        self.worker.stop()
        self.worker.join(3)
        self.destroy()


def run(ctx: AppContext) -> None:
    App(ctx).mainloop()
