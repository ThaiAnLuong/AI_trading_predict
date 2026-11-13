"""Settings and the About/Data tab."""
from __future__ import annotations

import os
import tkinter as tk
from dataclasses import fields
from tkinter import messagebox, simpledialog, ttk

from .. import __version__, dataops
from ..config import api_key
from ..market.registry import PROVIDERS
from ..money import parse_decimal
from ..settings import MARKET_PROVIDERS, Settings, save_settings
from .common import HelpTip, Table, check_row, form_row, show_error, show_info

ABOUT = """This is a paper-trading experiment. Every trade is a row in a local database. \
No money is involved, no brokerage is connected, and the app has no way to place a real order.

Simulated performance does not predict real results. The simulation leaves out real-world \
effects such as partial fills, taxes, broker fees and real FX spreads, and the AI is not \
expected or claimed to be profitable."""

RULES = """How the limits work
- One cash balance per account. Resets never add cash.
- Max buy = the smaller of cash available and the limit remaining this period.
- Buys use up the limit. Sale proceeds give it back, never below zero used.
- The period restarts at local midnight (daily) or Monday 00:00 local (weekly).
- Only CAD tickers can be bought unless you turn off "CAD tickers only" in Settings.
- Imported holdings use no cash, and their sale proceeds stay outside trading cash."""

TEXT_FIELDS = (
    ("Account rules (both accounts)", (
        ("starting_balance", "Starting balance (CAD)", "locked once trades exist"),
        ("period_limit", "Period buy limit (CAD)", "blank = starting balance"),
    )),
    ("Data", (
        ("max_quote_age_minutes", "Max quote age (min)", "older quotes pause the AI"),
        ("max_eod_age_days", "Max closing-price age (days)", "end-of-day sources"),
        ("max_fx_age_days", "Max FX age (days)", ""),
        ("refresh_minutes", "Price refresh (min)", ""),
        ("snapshot_minutes", "Snapshot interval (min)", ""),
        ("news_minutes", "News refresh (min)", ""),
    )),
    ("AI account limits", (
        ("max_trade_size", "Max trade size (CAD)", ""),
        ("max_daily_buys", "Max buys per day", ""),
        ("max_positions", "Max positions", ""),
        ("max_exposure_pct", "Max exposure (%)", ""),
        ("stop_loss_pct", "Stop-loss (%)", "below cost; rises with the highest price"),
        ("min_confidence", "Min confidence (0-1)", ""),
        ("news_lookback_hours", "News lookback (hours)", ""),
    )),
    ("Optional LLM review (ANTHROPIC_API_KEY)", (
        ("llm_model", "Model", ""),
        ("llm_max_reviews_per_day", "Max reviews per day", "each one is a paid API call"),
    )),
)
INT_FIELDS = {f.name for f in fields(Settings) if f.type == "int"}

# What each setting does, shown by the "?" next to it.
HELP = {
    "starting_balance": "The cash each account (yours and the AI's) starts the experiment "
        "with, in CAD. Locked once any trade exists, so both accounts stay comparable. To change "
        "it, use Reset experiment on the About and data tab.",
    "period_limit": "The most new money each account can put into stocks per day or week. "
        "Selling gives allowance back, but never more than you used. Blank means the starting "
        "balance. The reset only restores the allowance; it never adds cash.",
    "reset_period": "When the buy limit starts over. Daily: local midnight. Weekly: Monday "
        "00:00.",
    "allow_fractional": "Allow buying part of a share (up to 4 decimals), in both accounts. "
        "Off: whole shares only.",
    "cad_only": "Only CAD listings (.TO, .V, .NE) can be bought or imported. US companies are "
        "traded through their Canadian CDRs, such as GOOG.TO. US tickers on the watchlist are "
        "skipped.",
    "max_quote_age_minutes": "A live price older than this counts as stale. The AI pauses that "
        "stock, and a manual trade asks you to confirm and is flagged STALE in the trade log.",
    "max_eod_age_days": "For end-of-day sources such as Alpha Vantage: a closing price older "
        "than this many days counts as stale, which pauses the AI for that stock.",
    "max_fx_age_days": "Only matters with US tickers allowed: a USD/CAD rate older than this "
        "blocks US trades.",
    "refresh_minutes": "How often prices are fetched while the app is open. The AI makes its "
        "decisions right after each refresh.",
    "snapshot_minutes": "How often both account values are recorded. Snapshots feed the "
        "performance numbers and the Compare chart.",
    "news_minutes": "How often the news feeds on the News tab are read.",
    "market_provider": "Where prices come from. Yahoo: free, unofficial, no key. Alpha "
        "Vantage: needs a key, closing prices only, about 25 calls a day. Twelve Data: needs a "
        "key, and Canada needs a paid plan.",
    "max_trade_size": "The most CAD the AI spends on one buy, including buying more of a stock "
        "it already holds. It may spend less to stay inside cash, limit and exposure rules.",
    "max_daily_buys": "The most buys the AI makes in one day, counting buy-more orders. Sells "
        "are never limited.",
    "max_positions": "The most different stocks the AI holds at once. Buying more of a stock "
        "it already holds does not count as a new one.",
    "max_exposure_pct": "The most of the AI account's value that can be in stocks; the rest "
        "stays as cash. 80 means at least 20% cash.",
    "stop_loss_pct": "The AI sells if the price falls this % below what it paid. Once the "
        "price rises, the stop moves up to this % below the highest price since buying. The "
        "sell target is twice this % above cost (8% -> +16%).",
    "min_confidence": "The AI only buys when its confidence (0 to 1) is at least this. A basic "
        "uptrend scores 0.55; high volume and supporting news add to it.",
    "news_lookback_hours": "How far back news and market-link headlines count when the AI "
        "decides.",
    "market_links_enabled": "Use the 30 market links (Market links tab): sector news can stop "
        "an AI buy or raise its confidence. It never starts a trade by itself. Off: the tab "
        "still shows the links, for information only.",
    "llm_review_enabled": "Before each AI buy, ask Claude to approve or veto it. Needs "
        "ANTHROPIC_API_KEY in the source API file, and each review is a paid API call. Not "
        "tested against the real service yet. If the review fails, the AI does not buy.",
    "llm_model": "The Claude model used for the optional buy review.",
    "llm_max_reviews_per_day": "The most paid Claude reviews per day. Once reached, further "
        "AI buys that day are skipped.",
    "slippage": "Simulates the gap between the quoted price and the price you would really "
        "get. Buys fill a little higher and sells a little lower. abs: an amount per share in "
        "the stock's currency. pct: a % of the price. Not a broker fee.",
    "default_category": "The slippage category for any stock not given one on the Watchlist "
        "tab.",
}


class SettingsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app, self.ctx = app, app.ctx
        self.vars: dict[str, tk.StringVar] = {}
        self.columnconfigure((0, 1), weight=1, uniform="col")
        ttk.Label(self, text="Settings", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        left, right = ttk.Frame(self), ttk.Frame(self)
        left.grid(row=1, column=0, sticky="nwe", padx=(0, 8))
        right.grid(row=1, column=1, sticky="nwe")

        for i, (title, rows) in enumerate(TEXT_FIELDS):
            box = ttk.LabelFrame(left if i < 2 else right, text=title, padding=8)
            box.pack(fill="x", pady=(6, 0))
            box.columnconfigure(1, weight=1)
            for r, (key, label, hint) in enumerate(rows):
                self.vars[key] = tk.StringVar()
                form_row(box, r, label, ttk.Entry(box, textvariable=self.vars[key], width=18),
                         hint, HELP.get(key, ""))
            if i == 0:
                self.period = tk.StringVar()
                self.fractional = tk.BooleanVar()
                form_row(box, 2, "Limit resets", ttk.Combobox(
                    box, textvariable=self.period, values=("daily", "weekly"), state="readonly",
                    width=16), help=HELP["reset_period"])
                check_row(box, 3, "Allow fractional shares (up to 4 decimals)", self.fractional,
                          HELP["allow_fractional"])
                self.cad_only = tk.BooleanVar()
                check_row(box, 4, "CAD tickers only (blocks buying US listings)", self.cad_only,
                          HELP["cad_only"])
            if i == 1:
                self.provider = tk.StringVar()
                form_row(box, 6, "Market data", ttk.Combobox(
                    box, textvariable=self.provider, values=MARKET_PROVIDERS, state="readonly",
                    width=16), help=HELP["market_provider"])
                self.key_label = ttk.Label(box, style="Muted.TLabel")
                self.key_label.grid(row=7, column=0, columnspan=3, sticky="w")
            if i == 2:
                self.links = tk.BooleanVar()
                check_row(box, len(rows), "Use market links (sector news can veto or support a buy)",
                          self.links, HELP["market_links_enabled"])
            if i == 3:
                self.llm = tk.BooleanVar()
                check_row(box, 2, "Review AI buy candidates with Claude (not live-tested)",
                          self.llm, HELP["llm_review_enabled"])

        slip = ttk.LabelFrame(left, text="Simulated spread/slippage (not a broker fee)", padding=8)
        slip.pack(fill="x", pady=(6, 0))
        slip.columnconfigure(0, weight=1)
        HelpTip(slip, HELP["slippage"]).grid(row=0, column=5, sticky="nw", padx=(6, 0))
        self.slip_table = Table(slip, [("Category", 110), ("Mode", 60), ("Value", 70)], height=3)
        self.slip_table.grid(row=0, column=0, columnspan=5, sticky="we")
        self.slip_table.on_select(self.pick_category)
        self.cat_name, self.cat_mode, self.cat_value = tk.StringVar(), tk.StringVar(value="abs"), tk.StringVar()
        ttk.Entry(slip, textvariable=self.cat_name, width=12).grid(row=1, column=0, sticky="we", pady=4)
        ttk.Combobox(slip, textvariable=self.cat_mode, values=("abs", "pct"), state="readonly",
                     width=5).grid(row=1, column=1, padx=4)
        ttk.Entry(slip, textvariable=self.cat_value, width=8).grid(row=1, column=2)
        ttk.Button(slip, text="Add/Update", command=self.save_category).grid(row=1, column=3, padx=4)
        ttk.Button(slip, text="Remove", command=self.remove_category).grid(row=1, column=4)
        ttk.Label(slip, style="Muted.TLabel", text="abs = per share in the ticker's currency; "
                  "pct = % of price. Assign tickers on the Watchlist tab.").grid(
            row=2, column=0, columnspan=5, sticky="w")
        self.default_cat = tk.StringVar()
        self.default_box = ttk.Combobox(slip, textvariable=self.default_cat, state="readonly", width=12)
        ttk.Label(slip, text="Default category").grid(row=3, column=0, sticky="w")
        self.default_box.grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))
        HelpTip(slip, HELP["default_category"]).grid(row=3, column=3, sticky="w", pady=(4, 0))

        ttk.Button(right, text="Save settings", command=self.save).pack(anchor="e", pady=(10, 0))
        ttk.Label(right, style="Muted.TLabel", wraplength=420, justify="left", text=(
            "Changes apply to both accounts immediately and are recorded in the settings audit "
            "log, which is included in exports.")).pack(anchor="e")
        self.loaded = False

    def refresh(self) -> None:
        s = self.ctx.settings()
        if not self.loaded:  # do not overwrite edits in progress on background refreshes
            for key, var in self.vars.items():
                var.set(str(getattr(s, key)))
            self.period.set(s.reset_period)
            self.fractional.set(s.allow_fractional)
            self.cad_only.set(s.cad_only)
            self.provider.set(s.market_provider)
            self.llm.set(s.llm_review_enabled)
            self.links.set(s.market_links_enabled)
            self.default_cat.set(s.default_category)
            self.loaded = True
        self.draw_categories(s)
        status = [f"{env}: {'set' if api_key(env) else 'not set'}"
                  for _, env in PROVIDERS.values() if env]
        note = "yahoo needs no key but is unofficial and can stop at any time.\n"
        self.key_label.configure(text=note + " | ".join(status))

    def draw_categories(self, s: Settings) -> None:
        self.slip_table.set_rows([(name, (name, rule["mode"], rule["value"]))
                                  for name, rule in sorted(s.slippage_categories.items())])
        self.default_box["values"] = sorted(s.slippage_categories)

    def pick_category(self) -> None:
        name = self.slip_table.selected()
        if name:
            rule = self.ctx.settings().slippage_categories[name]
            self.cat_name.set(name)
            self.cat_mode.set(rule["mode"])
            self.cat_value.set(rule["value"])

    def save_category(self) -> None:
        name = self.cat_name.get().strip().lower()
        if not name:
            show_error(self, "Enter a category name.")
            return
        s = self.ctx.settings()
        s.slippage_categories[name] = {"mode": self.cat_mode.get(), "value": self.cat_value.get().strip()}
        self.store(s)

    def remove_category(self) -> None:
        name = self.slip_table.selected()
        if not name:
            show_error(self, "Select a category first.")
            return
        s = self.ctx.settings()
        if name == s.default_category or name in s.ticker_categories.values():
            show_error(self, "That category is in use.")
            return
        del s.slippage_categories[name]
        self.store(s)

    def store(self, s: Settings) -> bool:
        try:
            save_settings(self.ctx.db, s, self.ctx.clock)
        except ValueError as exc:
            show_error(self, str(exc))
            return False
        self.app.refresh_all()
        return True

    def save(self) -> None:
        s = self.ctx.settings()
        try:
            for key, var in self.vars.items():
                value = var.get().strip()
                if key in INT_FIELDS:
                    number = parse_decimal(value, key.replace("_", " ").capitalize())
                    if number != int(number):
                        raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a whole number.")
                    value = int(number)
                setattr(s, key, value)
        except ValueError as exc:
            show_error(self, str(exc))
            return
        s.reset_period = self.period.get()
        s.allow_fractional = self.fractional.get()
        s.cad_only = self.cad_only.get()
        s.market_provider = self.provider.get()
        s.llm_review_enabled = self.llm.get()
        s.market_links_enabled = self.links.get()
        s.default_category = self.default_cat.get()
        if self.store(s):
            self.app.worker.request_refresh()
            show_info(self, "Settings saved.")


class AboutTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=14)
        self.app, self.ctx = app, app.ctx
        ttk.Label(self, text="About this experiment", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, text=f"Paper Trader version {__version__}", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(self, text=ABOUT, wraplength=900, justify="left").pack(anchor="w", pady=8)
        ttk.Label(self, text=RULES, wraplength=900, justify="left").pack(anchor="w", pady=4)
        box = ttk.LabelFrame(self, text="Your data", padding=10)
        box.pack(fill="x", pady=10)
        ttk.Label(box, text=f"Database: {self.ctx.paths.db}", style="Muted.TLabel").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(anchor="w", pady=(6, 0))
        ttk.Button(row, text="Back up now", command=self.backup).pack(side="left")
        ttk.Button(row, text="Export everything to CSV", command=self.export).pack(side="left", padx=6)
        ttk.Button(row, text="Open data folder", command=self.open_folder).pack(side="left")
        ttk.Button(row, text="Reset experiment...", command=self.reset).pack(side="left", padx=(24, 0))

    def refresh(self) -> None:
        pass

    def backup(self) -> None:
        show_info(self, f"Backup saved and verified:\n{dataops.backup_now(self.ctx)}")

    def export(self) -> None:
        show_info(self, f"Exported to:\n{dataops.export_all(self.ctx)}")

    def open_folder(self) -> None:
        os.startfile(self.ctx.paths.data_dir)

    def reset(self) -> None:
        choice = messagebox.askyesnocancel(
            "Reset experiment",
            "Reset clears all trades, snapshots and AI decisions for both accounts.\n\n"
            "Create a backup and CSV export first? (Recommended)", parent=self)
        if choice is None:
            return
        if choice:
            backup = dataops.backup_now(self.ctx, "before_reset")
            export = dataops.export_all(self.ctx)
            show_info(self, f"Backup: {backup}\nExport: {export}")
        typed = simpledialog.askstring(
            "Confirm reset", "Type RESET to clear the experiment. This cannot be undone here.",
            parent=self)
        if typed != "RESET":
            show_info(self, "Nothing was changed.")
            return
        balance = simpledialog.askstring(
            "Starting balance", "New starting balance in CAD (leave blank to keep the current one):",
            parent=self)
        balance = (balance or "").strip() or None
        if balance:
            try:
                if parse_decimal(balance, "Starting balance") <= 0:
                    raise ValueError("Starting balance must be greater than zero.")
            except ValueError as exc:
                show_error(self, f"{exc} Nothing was changed.")
                return
        dataops.reset_experiment(self.ctx, balance)
        self.app.tabs_loaded_reset()
        self.app.worker.request_refresh()
        self.app.refresh_all()
        show_info(self, "The experiment was reset.")
