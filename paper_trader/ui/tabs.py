"""Watchlist, trade log, AI decisions, news and comparison tabs."""
from __future__ import annotations

import json
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from .. import dataops
from ..money import fmt_money, fmt_pct, fmt_price, fmt_qty, fmt_signed
from ..news.registry import USER_KINDS
from ..performance import ACCOUNT_LABELS, comparison, matched_series
from ..settings import save_settings
from ..symbols import cad_only_message, currency_for, normalize_ticker
from ..timeutil import local_str, to_iso
from ..valuation import load_quotes, load_state, load_trades, quote_status
from .chart import ValueChart
from ..ledger import realized_by_sale
from .common import Table, bold_num, show_error, show_info, sign_tag, text_box


def details_box(parent, height=8) -> tk.Text:
    return text_box(parent, height=height)


def set_text(box: tk.Text, text: str) -> None:
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    box.configure(state="disabled")


class WatchlistTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app, self.ctx = app, app.ctx
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text="Watchlist", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="The AI only considers these tickers and anything it already holds. "
                  "TSX: RY.TO.  Canadian version of a US company (CDR): GOOG.TO.  "
                  "TSX Venture: ABC.V.  Cboe Canada: GOOG.NE.  Yellow rows: held by the AI account.",
                  style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 6))
        self.table = Table(self, [("Ticker", 90), ("Company name", 170), ("AI holds", 65),
                                  ("Ref price", 90), ("Ccy", 45), ("Exchange", 80), ("Price time", 125),
                                  ("Data", 130), ("Source and timing", 300), ("Slippage", 80)],
                           height=14)
        self.table.grid(row=2, column=0, sticky="nsew")
        self.table.on_select(self.selected)

        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="we", pady=(8, 0))
        self.ticker, self.name = tk.StringVar(), tk.StringVar()
        ttk.Label(bar, text="Ticker").pack(side="left")
        ttk.Entry(bar, textvariable=self.ticker, width=12).pack(side="left", padx=4)
        ttk.Label(bar, text="Company name (used to match news)").pack(side="left", padx=(8, 0))
        ttk.Entry(bar, textvariable=self.name, width=24).pack(side="left", padx=4)
        ttk.Button(bar, text="Add", command=self.add).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove selected", command=self.remove).pack(side="left", padx=4)
        self.category = tk.StringVar()
        self.category_box = ttk.Combobox(bar, textvariable=self.category, width=10, state="readonly")
        self.category_box.pack(side="right", padx=4)
        ttk.Button(bar, text="Set slippage category", command=self.set_category).pack(side="right")
        self.error = ttk.Label(self, style="Muted.TLabel", wraplength=1100)
        self.error.grid(row=4, column=0, sticky="w", pady=(4, 0))

    def refresh(self) -> None:
        conn = self.ctx.db.conn()
        s = self.ctx.settings()
        now = self.ctx.clock()
        quotes = load_quotes(conn)
        ai_held = set(load_state(conn, "AI", s).positions)
        rows = []
        for w in conn.execute("SELECT * FROM watchlist ORDER BY ticker"):
            t = w["ticker"]
            q = quotes.get(t)
            held = t in ai_held
            rows.append((t, (
                t, w["name"], "yes" if held else "", bold_num(fmt_price(q["price"])) if q is not None else "n/a",
                currency_for(t), (q["exchange"] if q is not None else None) or "?",
                local_str(q["quote_ts"]) if q is not None else "never",
                quote_status(q, s, now),
                (f"{q['source'] or ''} - {q['delay_note'] or ''}" if q is not None and q["source"]
                 else "no data yet"),
                s.ticker_categories.get(t, s.default_category)), "held" if held else ""))
        self.table.set_rows(rows)
        self.category_box["values"] = sorted(s.slippage_categories)
        problems = [f"{t}: {q['error']}" for t, q in quotes.items()
                    if q["status"] != "ok" and q["error"]]
        self.error.configure(text=" | ".join(problems[:4]))

    def selected(self) -> None:
        ticker = self.table.selected()
        if ticker:
            s = self.ctx.settings()
            self.category.set(s.ticker_categories.get(ticker, s.default_category))

    def add(self) -> None:
        try:
            ticker = normalize_ticker(self.ticker.get())
        except ValueError as exc:
            show_error(self, str(exc))
            return
        if self.ctx.settings().cad_only and currency_for(ticker) != "CAD":
            show_error(self, cad_only_message(ticker))
            return
        self.ctx.db.conn().execute(
            "INSERT OR REPLACE INTO watchlist(ticker, name, added_ts) VALUES (?,?,?)",
            (ticker, self.name.get().strip(), to_iso(self.ctx.clock())))
        self.ticker.set("")
        self.name.set("")
        self.app.worker.request_refresh()
        self.refresh()

    def remove(self) -> None:
        ticker = self.table.selected()
        if not ticker:
            show_error(self, "Select a ticker first.")
            return
        self.ctx.db.conn().execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
        self.refresh()

    def set_category(self) -> None:
        ticker = self.table.selected()
        if not ticker or not self.category.get():
            show_error(self, "Select a ticker and a category first.")
            return
        s = self.ctx.settings()
        s.ticker_categories[ticker] = self.category.get()
        save_settings(self.ctx.db, s, self.ctx.clock)
        self.refresh()


class TradeLogTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app, self.ctx = app, app.ctx
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text="Trade log (simulated)", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="we", pady=6)
        self.account, self.side = tk.StringVar(value="All"), tk.StringVar(value="All")
        self.ticker, self.date_from, self.date_to = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(bar, text="Account").pack(side="left")
        ttk.Combobox(bar, textvariable=self.account, values=("All", "My account", "AI account"),
                     state="readonly", width=11).pack(side="left", padx=4)
        ttk.Label(bar, text="Ticker").pack(side="left")
        ttk.Entry(bar, textvariable=self.ticker, width=10).pack(side="left", padx=4)
        ttk.Label(bar, text="Action").pack(side="left")
        ttk.Combobox(bar, textvariable=self.side, values=("All", "BUY", "SELL", "IMPORT"),
                     state="readonly", width=7).pack(side="left", padx=4)
        ttk.Label(bar, text="From").pack(side="left")
        ttk.Entry(bar, textvariable=self.date_from, width=11).pack(side="left", padx=4)
        ttk.Label(bar, text="To").pack(side="left")
        ttk.Entry(bar, textvariable=self.date_to, width=11).pack(side="left", padx=4)
        ttk.Label(bar, text="(YYYY-MM-DD)", style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Apply", command=self.apply).pack(side="left", padx=6)
        ttk.Button(bar, text="Export these to CSV", command=self.export).pack(side="right")
        self.table = Table(self, [("Time", 125), ("Account", 80), ("By", 65), ("Action", 60),
                                  ("Ticker", 80), ("Qty", 60), ("Ccy", 40), ("Ref price", 85),
                                  ("Sim fill", 85), ("USD/CAD", 65), ("Cash CAD", 90),
                                  ("Realized", 100), ("Group", 75), ("Flags", 55), ("Reason", 220)],
                           height=16)
        self.table.grid(row=2, column=0, sticky="nsew")
        self.count = ttk.Label(self, style="Muted.TLabel")
        self.count.grid(row=3, column=0, sticky="w")

    def filter(self):
        account = {"My account": "MY", "AI account": "AI"}.get(self.account.get(), "")
        side = "" if self.side.get() == "All" else self.side.get()
        return dataops.trade_filter(account, self.ticker.get(), side, self.date_from.get(),
                                    self.date_to.get())

    def apply(self) -> None:
        try:
            self.filter()
        except ValueError as exc:
            show_error(self, str(exc))
            return
        self.refresh()

    def refresh(self) -> None:
        try:
            where, args = self.filter()
        except ValueError:
            return  # a half-typed date: keep the current rows until Apply is pressed
        conn = self.ctx.db.conn()
        start = self.ctx.settings().start
        realized = {}
        for account in ACCOUNT_LABELS:  # sale results need each account's full history
            realized.update(realized_by_sale(start, load_trades(conn, account)))
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY id DESC LIMIT 2000", args).fetchall()
        table_rows = []
        for r in rows:
            gain, currency = realized.get(r["id"], (None, ""))
            table_rows.append((str(r["id"]), (
                local_str(r["ts"]), ACCOUNT_LABELS[r["account"]], r["source"], r["side"], r["ticker"],
                fmt_qty(r["quantity"]), r["currency"], bold_num(fmt_price(r["reference_price"])),
                fmt_price(r["fill_price"]), r["fx_rate"] or "-",
                r["cash_delta"] if r["sleeve"] == "TRADING" else "none",
                fmt_signed(gain, currency) if gain is not None else "",
                "Imported" if r["sleeve"] == "IMPORTED" else "Trading",
                "STALE" if r["quote_stale"] else "", r["reason"]), sign_tag(gain)))
        self.table.set_rows(table_rows)
        self.count.configure(text=f"{len(rows)} entries shown.  Green: sale at a profit.  "
                                  "Red: sale at a loss.  Buys are not coloured.")

    def export(self) -> None:
        try:
            where, args = self.filter()
        except ValueError as exc:
            show_error(self, str(exc))
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", initialdir=self.ctx.paths.exports,
            initialfile="trades.csv", filetypes=[("CSV", "*.csv")])
        if path:
            dataops.export_trades(self.ctx.db.conn(), Path(path), where, args)
            show_info(self, f"Saved {path}")


class DecisionsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app, self.ctx = app, app.ctx
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text="AI decision log", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.table = Table(self, [("Time", 125), ("Ticker", 80), ("Action", 55), ("Status", 85),
                                  ("Qty", 55), ("Ref price", 85), ("Sim fill", 85), ("Ccy", 40),
                                  ("Confidence", 80), ("Explanation", 430)], height=13)
        self.table.grid(row=1, column=0, sticky="nsew", pady=6)
        self.table.on_select(self.show)
        self.detail = details_box(self, 9)
        self.detail.grid(row=2, column=0, sticky="we")

    def refresh(self) -> None:
        rows = self.ctx.db.conn().execute(
            "SELECT * FROM ai_decisions ORDER BY id DESC LIMIT 1000").fetchall()
        self.table.set_rows([(str(r["id"]), (
            local_str(r["ts"]), r["ticker"], r["action"], r["status"],
            fmt_qty(r["quantity"]) if r["quantity"] else "-",
            bold_num(fmt_price(r["reference_price"])) if r["reference_price"] else "-",
            fmt_price(r["simulated_fill_price"]) if r["simulated_fill_price"] else "-",
            r["currency"] or "-", r["confidence"] or "n/a", r["explanation"])) for r in rows])

    def show(self) -> None:
        chosen = self.table.selected()
        if not chosen:
            return
        r = self.ctx.db.conn().execute("SELECT * FROM ai_decisions WHERE id = ?", (chosen,)).fetchone()
        sources = json.loads(r["data_sources"])
        lines = [r["explanation"]]
        if r["rejection_reason"]:
            lines.append(f"Rejected by the rules: {r['rejection_reason']}")
        lines.append(f"Strategy: {r['strategy']}")
        if sources.get("quote"):
            q = sources["quote"]
            lines.append(f"Price: {q['source']} at {local_str(q['quote_ts'])}; "
                         f"daily bars used: {sources.get('daily_bars')}")
        if sources.get("metrics"):
            lines.append("Metrics: " + ", ".join(f"{k} {v}" for k, v in sources["metrics"].items()))
        if sources.get("llm_review"):
            lines.append(f"LLM review: {sources['llm_review']}")
        for n in sources.get("news_unverified", []):
            lines.append(f"Unverified news: {n['title']} ({n['source']}, "
                         f"{local_str(n['published'])}) {', '.join(n['labels'])}")
        set_text(self.detail, "\n".join(lines))


class NewsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app, self.ctx = app, app.ctx
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text="News and signals", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="Everything here is an unverified signal, not a fact. It can adjust or "
                  "veto a decision but cannot start a trade.", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 6))
        book = ttk.Notebook(self)
        book.grid(row=2, column=0, sticky="nsew")

        items = ttk.Frame(book, padding=6)
        items.columnconfigure(0, weight=1)
        items.rowconfigure(0, weight=1)
        book.add(items, text="Items")
        self.items = Table(items, [("Published", 125), ("Source", 170), ("Tickers", 110),
                                   ("Labels", 190), ("Title", 480)], height=12)
        self.items.grid(row=0, column=0, sticky="nsew")
        self.items.on_select(self.show)
        self.detail = details_box(items, 5)
        self.detail.grid(row=1, column=0, sticky="we", pady=(6, 0))
        self.open_button = ttk.Button(items, text="Open link in browser", command=self.open_link)
        self.open_button.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.url = ""

        sources = ttk.Frame(book, padding=6)
        sources.columnconfigure(0, weight=1)
        sources.rowconfigure(0, weight=1)
        book.add(sources, text="Sources")
        self.sources = Table(sources, [("Name", 200), ("Type", 70), ("URL", 400), ("On", 40),
                                       ("Last fetched", 125), ("Last result", 220)], height=10)
        self.sources.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(sources)
        bar.grid(row=1, column=0, sticky="we", pady=(6, 0))
        self.src_name, self.src_kind, self.src_url = tk.StringVar(), tk.StringVar(value="rss"), tk.StringVar()
        ttk.Label(bar, text="Name").pack(side="left")
        ttk.Entry(bar, textvariable=self.src_name, width=20).pack(side="left", padx=4)
        ttk.Combobox(bar, textvariable=self.src_kind, values=USER_KINDS, state="readonly",
                     width=8).pack(side="left", padx=4)
        ttk.Label(bar, text="Feed URL").pack(side="left")
        ttk.Entry(bar, textvariable=self.src_url, width=44).pack(side="left", padx=4)
        ttk.Button(bar, text="Add", command=self.add_source).pack(side="left", padx=4)
        ttk.Button(bar, text="On/Off", command=self.toggle_source).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove", command=self.remove_source).pack(side="left", padx=4)
        ttk.Label(sources, text="Only add feeds whose terms allow this use. The app does not "
                  "scrape web pages.", style="Muted.TLabel").grid(row=2, column=0, sticky="w")

        cats = ttk.Frame(book, padding=6)
        cats.columnconfigure(0, weight=1)
        cats.rowconfigure(0, weight=1)
        book.add(cats, text="Signal categories")
        self.cats = Table(cats, [("Category", 170), ("Effect", 80), ("On", 40), ("Keywords", 640)],
                          height=10)
        self.cats.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(cats)
        bar.grid(row=1, column=0, sticky="we", pady=(6, 0))
        self.cat_name, self.cat_words, self.cat_effect = tk.StringVar(), tk.StringVar(), tk.StringVar(value="neutral")
        ttk.Label(bar, text="Name").pack(side="left")
        ttk.Entry(bar, textvariable=self.cat_name, width=18).pack(side="left", padx=4)
        ttk.Label(bar, text="Keywords (comma separated)").pack(side="left")
        ttk.Entry(bar, textvariable=self.cat_words, width=36).pack(side="left", padx=4)
        ttk.Combobox(bar, textvariable=self.cat_effect, values=("positive", "neutral", "negative"),
                     state="readonly", width=8).pack(side="left", padx=4)
        ttk.Button(bar, text="Add", command=self.add_category).pack(side="left", padx=4)
        ttk.Button(bar, text="On/Off", command=self.toggle_category).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove", command=self.remove_category).pack(side="left", padx=4)

    def refresh(self) -> None:
        conn = self.ctx.db.conn()
        rows = conn.execute("SELECT * FROM news_items ORDER BY published_ts DESC LIMIT 500").fetchall()
        self.items.set_rows([(str(r["id"]), (
            local_str(r["published_ts"]), r["source"], ", ".join(json.loads(r["tickers"])) or "-",
            ", ".join(json.loads(r["labels"])) or "-", r["title"])) for r in rows])
        self.sources.set_rows([(str(r["id"]), (
            r["name"], r["kind"], r["url"] or "-", "yes" if r["enabled"] else "no",
            local_str(r["last_fetch_ts"]), r["last_status"]))
            for r in conn.execute("SELECT * FROM news_sources ORDER BY name")])
        effect = {1: "positive", 0: "neutral", -1: "negative"}
        self.cats.set_rows([(r["name"], (
            r["name"], effect[r["direction"]], "yes" if r["enabled"] else "no",
            ", ".join(json.loads(r["keywords"]))))
            for r in conn.execute("SELECT * FROM signal_categories ORDER BY name")])

    def show(self) -> None:
        chosen = self.items.selected()
        if not chosen:
            return
        r = self.ctx.db.conn().execute("SELECT * FROM news_items WHERE id = ?", (chosen,)).fetchone()
        self.url = r["url"]
        set_text(self.detail, f"{r['title']}\n{r['summary']}\nKeywords: "
                 f"{', '.join(json.loads(r['keywords'])) or '-'}\n{r['url']}")

    def open_link(self) -> None:
        if self.url.startswith("https://") and "example.invalid" not in self.url:
            webbrowser.open(self.url)

    def add_source(self) -> None:
        name, kind, url = self.src_name.get().strip(), self.src_kind.get(), self.src_url.get().strip()
        if not name:
            show_error(self, "Enter a name for the source.")
            return
        if kind == "rss" and not url.lower().startswith("https://"):
            show_error(self, "An RSS source needs an https:// feed URL.")
            return
        conn = self.ctx.db.conn()
        if conn.execute("SELECT 1 FROM news_sources WHERE name = ?", (name,)).fetchone():
            show_error(self, "A source with that name already exists.")
            return
        conn.execute("INSERT INTO news_sources(name, kind, url) VALUES (?,?,?)", (name, kind, url))
        self.src_name.set("")
        self.src_url.set("")
        self.refresh()

    def toggle_source(self) -> None:
        chosen = self.sources.selected()
        if chosen:
            self.ctx.db.conn().execute(
                "UPDATE news_sources SET enabled = 1 - enabled WHERE id = ?", (chosen,))
            self.refresh()

    def remove_source(self) -> None:
        chosen = self.sources.selected()
        if chosen:
            self.ctx.db.conn().execute("DELETE FROM news_sources WHERE id = ?", (chosen,))
            self.refresh()

    def add_category(self) -> None:
        name = self.cat_name.get().strip().lower().replace(" ", "_")
        words = [w.strip().lower() for w in self.cat_words.get().split(",") if w.strip()]
        if not name or not words:
            show_error(self, "Enter a category name and at least one keyword.")
            return
        direction = {"positive": 1, "neutral": 0, "negative": -1}[self.cat_effect.get()]
        self.ctx.db.conn().execute(
            "INSERT OR REPLACE INTO signal_categories(name, keywords, direction, enabled)"
            " VALUES (?,?,?,1)", (name, json.dumps(words), direction))
        self.cat_name.set("")
        self.cat_words.set("")
        self.refresh()

    def toggle_category(self) -> None:
        chosen = self.cats.selected()
        if chosen:
            self.ctx.db.conn().execute(
                "UPDATE signal_categories SET enabled = 1 - enabled WHERE name = ?", (chosen,))
            self.refresh()

    def remove_category(self) -> None:
        chosen = self.cats.selected()
        if chosen:
            self.ctx.db.conn().execute("DELETE FROM signal_categories WHERE name = ?", (chosen,))
            self.refresh()


class CompareTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app, self.ctx = app, app.ctx
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text="My account vs AI account (simulated)", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        self.subtitle = ttk.Label(self, style="Muted.TLabel")
        self.subtitle.grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.chart = ValueChart(self)
        self.chart.grid(row=2, column=0, sticky="nsew")
        self.table = Table(self, [("Account", 110), ("Value CAD", 110), ("Return", 75),
                                  ("Realized", 100), ("Unrealized", 100), ("Max drawdown", 100),
                                  ("Trades", 60), ("Cash held", 110), ("Cash %", 65)], height=2)
        self.table.grid(row=3, column=0, sticky="we", pady=(8, 0))
        bar = ttk.Frame(self)
        bar.grid(row=4, column=0, sticky="we", pady=(6, 0))
        ttk.Button(bar, text="Export performance history to CSV", command=self.export).pack(side="left")
        ttk.Label(bar, text="Simulated results do not predict real results.",
                  style="Muted.TLabel").pack(side="right")

    def refresh(self) -> None:
        conn = self.ctx.db.conn()
        s = self.ctx.settings()
        points = matched_series(conn)
        self.chart.set_data([p.date for p in points], [float(p.my["total_value"]) for p in points],
                            [float(p.ai["total_value"]) for p in points], float(s.start))
        day, end = dataops.experiment_day(self.ctx)
        ending = end.astimezone().strftime("%Y-%m-%d") if end else "?"
        partial = sum(1 for p in points if "partial" in (p.my["data_quality"], p.ai["data_quality"]))
        note = f"; {partial} used partial prices" if partial else ""
        self.subtitle.configure(text=(
            f"Day {day} of about 152 (around {ending}). {len(points)} matching date(s){note}. "
            f"Both started with {fmt_money(s.start)} under the same rules. "
            "Imported holdings are excluded."))
        self.table.set_rows([(r.account, (
            ACCOUNT_LABELS[r.account], fmt_money(r.total_value), fmt_pct(r.return_pct),
            fmt_signed(r.realized), fmt_signed(r.unrealized), f"{r.drawdown_pct:.2f}%", r.trades,
            fmt_money(r.cash), f"{r.cash_pct:.1f}%")) for r in comparison(conn, s)])

    def export(self) -> None:
        folder = self.ctx.paths.exports
        conn = self.ctx.db.conn()
        dataops.export_performance(conn, folder / "performance_snapshots.csv")
        dataops.export_comparison(conn, folder / "comparison_daily.csv")
        show_info(self, f"Saved performance_snapshots.csv and comparison_daily.csv in\n{folder}")
