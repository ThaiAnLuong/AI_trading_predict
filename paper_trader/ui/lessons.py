"""The AI lessons tab: what the AI account's finished trades show. AI account only."""
from __future__ import annotations

import os
from tkinter import ttk

from ..ai.lessons import MIN_SAMPLE, build_lessons, write_lessons_file
from ..money import fmt_pct, fmt_qty, fmt_signed
from ..timeutil import local_str
from .common import Table, show_error
from .tabs import details_box, set_text

SCORE_COLUMNS = [("Type", 70), ("Name", 190), ("Trades", 60), ("Win rate", 70), ("Average", 80),
                 ("Total CAD", 100), ("vs all trades", 95), ("Verdict", 130)]


def score_row(s) -> tuple:
    return (s.kind, s.name, s.trades, f"{s.win_rate:.0f}%", fmt_pct(s.average_pct),
            fmt_signed(s.total_gain), fmt_pct(s.versus_all) if s.versus_all is not None else "-",
            s.verdict)


class LessonsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app, self.ctx = app, app.ctx
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self.finished = {}
        ttk.Label(self, text="AI lessons (simulated, AI account only)", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(self, style="Muted.TLabel", text=(
            f"Patterns, not causes. Under {MIN_SAMPLE} trades is usually luck. "
            "The AI does not change its rules from this.")).grid(row=1, column=0, sticky="w")
        self.summary = ttk.Label(self)
        self.summary.grid(row=2, column=0, sticky="w", pady=(4, 6))

        book = ttk.Notebook(self)
        book.grid(row=3, column=0, sticky="nsew")

        trades = ttk.Frame(book, padding=6)
        trades.columnconfigure(0, weight=1)
        trades.rowconfigure(0, weight=1)
        book.add(trades, text="Finished trades")
        self.trades = Table(trades, [("Closed", 125), ("Ticker", 85), ("Shares", 60), ("Gain CAD", 95),
                                     ("Gain %", 70), ("Days held", 70), ("How it ended", 95),
                                     ("News at buy", 80), ("Why it bought", 330)], height=11)
        self.trades.grid(row=0, column=0, sticky="nsew")
        self.trades.on_select(self.show)
        self.detail = details_box(trades, 7)
        self.detail.grid(row=1, column=0, sticky="we", pady=(6, 0))

        signals = ttk.Frame(book, padding=6)
        signals.columnconfigure(0, weight=1)
        signals.rowconfigure(0, weight=1)
        book.add(signals, text="Keywords and events")
        self.signals = Table(signals, SCORE_COLUMNS, height=14)
        self.signals.grid(row=0, column=0, sticky="nsew")
        ttk.Label(signals, style="Muted.TLabel", text=(
            "News published before the AI bought. 'vs all trades' compares the average with every "
            "finished trade.")).grid(row=1, column=0, sticky="w", pady=(4, 0))

        trends = ttk.Frame(book, padding=6)
        trends.columnconfigure(0, weight=1)
        trends.rowconfigure(0, weight=1)
        book.add(trends, text="Trends over time")
        self.trends = Table(trends, SCORE_COLUMNS, height=14)
        self.trends.grid(row=0, column=0, sticky="nsew")

        bar = ttk.Frame(self)
        bar.grid(row=4, column=0, sticky="we", pady=(6, 0))
        ttk.Button(bar, text="Open text file", command=self.open_file).pack(side="left")
        ttk.Label(bar, text=str(self.ctx.paths.lessons), style="Muted.TLabel").pack(side="left", padx=8)

    def refresh(self) -> None:
        lessons = build_lessons(self.ctx.db.conn(), self.ctx.settings().news_lookback_hours)
        self.finished = {t.id: t for t in lessons.finished}
        o = lessons.overall
        held = f"  Still held, not scored: {', '.join(lessons.still_held)}." if lessons.still_held else ""
        self.summary.configure(text=(
            f"{o.trades} finished trade(s), {o.win_rate:.0f}% wins, average "
            f"{fmt_pct(o.average_pct)}, total {fmt_signed(o.total_gain)}.{held}"
            if o else f"No finished AI trades yet.{held}"))
        self.trades.set_rows([(t.id, (
            local_str(t.closed_ts), t.ticker, fmt_qty(t.quantity), f"{t.gain:+,.2f}",
            fmt_pct(t.gain_pct), t.days_held, t.exit_kind, len(t.news), t.entry_reason))
            for t in reversed(lessons.finished)])
        self.signals.set_rows([(f"{s.kind}:{s.name}", score_row(s)) for s in lessons.signals])
        self.trends.set_rows([(f"{s.kind}:{s.name}", score_row(s))
                              for s in lessons.months + lessons.exits])

    def show(self) -> None:
        trade = self.finished.get(self.trades.selected())
        if trade is None:
            return
        lines = [f"Why it bought: {trade.entry_reason}", f"Why it sold: {trade.exit_reason}"]
        lines += [f"News ({'market-wide' if n.market_wide else trade.ticker}, unverified): "
                  f"{n.title} [{', '.join(n.labels)}]" for n in trade.news]
        if not trade.news:
            lines.append("No labelled news was found before this buy.")
        set_text(self.detail, "\n".join(lines))

    def open_file(self) -> None:
        try:
            os.startfile(write_lessons_file(self.ctx))
        except OSError as exc:
            show_error(self, f"Could not open the file: {exc}")
