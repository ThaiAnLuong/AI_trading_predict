"""What the AI account's finished trades can teach. AI account only.

Everything here is worked out from the saved trades and news, so it always matches the
log and covers the whole experiment. It is a report for a person to read: nothing in
this module feeds back into the AI's decisions.

News is matched to a trade by time: anything with an event label published in the
lookback window before the buy, unless it names only other tickers. That includes
neutral, market-wide news such as war or gold stories, which is exactly the kind of item
the strategy itself ignores.

These are patterns, not causes.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from ..context import AppContext
from ..money import D, ZERO, fmt_pct, fmt_qty, fmt_signed
from ..timeutil import from_iso, local_str, to_iso, to_local

MIN_SAMPLE = 20  # below this, a scorecard row is labelled as not enough data
NOT_ENOUGH = "not enough data"


@dataclass(frozen=True)
class NewsAtEntry:
    title: str
    source: str
    published_ts: str
    labels: tuple[str, ...]
    keywords: tuple[str, ...]
    market_wide: bool


@dataclass
class FinishedTrade:
    id: str
    ticker: str
    quantity: Decimal
    opened_ts: str
    closed_ts: str
    cost: Decimal
    proceeds: Decimal
    entry_reason: str
    exit_reason: str
    news: list[NewsAtEntry] = field(default_factory=list)

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def gain_pct(self) -> Decimal:
        return self.gain / self.cost * 100

    @property
    def won(self) -> bool:
        return self.gain > 0

    @property
    def days_held(self) -> int:
        return (from_iso(self.closed_ts) - from_iso(self.opened_ts)).days

    @property
    def exit_kind(self) -> str:
        for prefix in ("Stop-loss", "Trend exit"):
            if self.exit_reason.startswith(prefix):
                return prefix
        return "Other"


def load_finished_trades(conn: sqlite3.Connection) -> tuple[list[FinishedTrade], list[str]]:
    """Pair the AI's buys and sells. A trade finishes when its position returns to zero.

    Returns (finished trades, tickers still held).
    """
    open_trades: dict[str, dict] = {}
    finished = []
    rows = conn.execute("SELECT * FROM trades WHERE account = 'AI' ORDER BY id")
    for t in rows:
        ticker, qty, cash = t["ticker"], D(t["quantity"]), D(t["cash_delta"])
        if t["side"] == "BUY":
            run = open_trades.setdefault(ticker, {
                "id": str(t["id"]), "opened": t["ts"], "entry": t["reason"], "held": ZERO,
                "bought": ZERO, "cost": ZERO, "proceeds": ZERO})
            run["held"] += qty
            run["bought"] += qty
            run["cost"] -= cash
            continue
        run = open_trades[ticker]
        run["held"] -= qty
        run["proceeds"] += cash
        if run["held"] <= 0:
            del open_trades[ticker]
            finished.append(FinishedTrade(run["id"], ticker, run["bought"], run["opened"], t["ts"],
                                          run["cost"], run["proceeds"], run["entry"], t["reason"]))
    return finished, sorted(open_trades)


def attach_news(conn: sqlite3.Connection, trades: list[FinishedTrade], lookback_hours: int) -> None:
    for trade in trades:
        opened = from_iso(trade.opened_ts)
        rows = conn.execute(
            "SELECT * FROM news_items WHERE published_ts >= ? AND published_ts <= ?"
            " ORDER BY published_ts DESC",
            (to_iso(opened - timedelta(hours=lookback_hours)), trade.opened_ts))
        for row in rows:
            labels, tickers = json.loads(row["labels"]), json.loads(row["tickers"])
            if labels and (not tickers or trade.ticker in tickers):
                trade.news.append(NewsAtEntry(
                    row["title"], row["source"], row["published_ts"], tuple(labels),
                    tuple(json.loads(row["keywords"])), not tickers))


@dataclass(frozen=True)
class Score:
    kind: str
    name: str
    trades: int
    wins: int
    average_pct: Decimal
    total_gain: Decimal
    versus_all: Decimal | None

    @property
    def win_rate(self) -> Decimal:
        return D(self.wins) / self.trades * 100

    @property
    def verdict(self) -> str:
        return NOT_ENOUGH if self.trades < MIN_SAMPLE else "possible pattern"


def _score(kind: str, name: str, trades: list[FinishedTrade], baseline: Decimal | None) -> Score:
    average = sum((t.gain_pct for t in trades), ZERO) / len(trades)
    return Score(kind, name, len(trades), sum(t.won for t in trades), average,
                 sum((t.gain for t in trades), ZERO),
                 None if baseline is None else average - baseline)


def _grouped(trades: list[FinishedTrade], key) -> dict[str, list[FinishedTrade]]:
    groups: dict[str, list[FinishedTrade]] = {}
    for trade in trades:
        for name in key(trade):
            groups.setdefault(name, []).append(trade)
    return groups


@dataclass
class Lessons:
    finished: list[FinishedTrade]
    still_held: list[str]
    overall: Score | None
    signals: list[Score]   # event labels and keywords present at entry
    exits: list[Score]
    months: list[Score]


def build_lessons(conn: sqlite3.Connection, lookback_hours: int) -> Lessons:
    finished, held = load_finished_trades(conn)
    attach_news(conn, finished, lookback_hours)
    if not finished:
        return Lessons([], held, None, [], [], [])
    overall = _score("All", "All finished trades", finished, None)
    base = overall.average_pct
    signals = []
    for kind, attr in (("Event", "labels"), ("Keyword", "keywords")):
        groups = _grouped(finished, lambda t: {x for n in t.news for x in getattr(n, attr)})
        signals += [_score(kind, name, items, base) for name, items in groups.items()]
    signals.sort(key=lambda s: (-s.trades, s.kind, s.name))
    exits = [_score("Exit", name, items, base)
             for name, items in sorted(_grouped(finished, lambda t: [t.exit_kind]).items())]
    months = [_score("Month", name, items, None) for name, items in sorted(_grouped(
        finished, lambda t: [to_local(from_iso(t.closed_ts)).strftime("%Y-%m")]).items())]
    return Lessons(finished, held, overall, signals, exits, months)


def _line(score: Score) -> str:
    versus = "" if score.versus_all is None else f", {fmt_pct(score.versus_all)} vs all trades"
    return (f"{score.name}: {score.trades} trade(s), {score.win_rate:.0f}% wins, average "
            f"{fmt_pct(score.average_pct)}, total {fmt_signed(score.total_gain)}{versus}")


def report_text(lessons: Lessons, generated_ts: str, demo: bool) -> str:
    lines = ["AI LESSONS - PAPER TRADING, SIMULATED, NO REAL MONEY"]
    if demo:
        lines.append("DEMO MODE: THIS IS ALL FAKE DATA")
    lines += [
        f"Updated: {local_str(generated_ts)}",
        "",
        "How to read this: these are patterns, not causes. A word appearing before a loss does",
        "not mean it caused the loss. Small samples are often luck, so anything under "
        f"{MIN_SAMPLE} trades",
        f"is marked '{NOT_ENOUGH}'. The AI does not read this file or change its rules from it.",
        "",
    ]
    if lessons.overall is None:
        lines.append("No finished AI trades yet.")
    else:
        lines += ["OVERALL", f"  {_line(lessons.overall)}", ""]
        lines.append("HOW TRADES ENDED")
        lines += [f"  {_line(s)}" for s in lessons.exits]
        lines += ["", "BY MONTH"]
        lines += [f"  {_line(s)}" for s in lessons.months]
        lines += ["", "NEWS PRESENT WHEN THE AI BOUGHT"]
        if not lessons.signals:
            lines.append("  No labelled news was found around these trades.")
        for s in lessons.signals:
            lines.append(f"  [{s.kind}] {_line(s)} [{s.verdict}]")
        lines += ["", "EVERY FINISHED TRADE, NEWEST FIRST"]
        for t in reversed(lessons.finished):
            lines += [
                "",
                f"{t.ticker}: {fmt_signed(t.gain)} ({fmt_pct(t.gain_pct)}), {fmt_qty(t.quantity)} "
                f"shares, held {t.days_held} day(s), {local_str(t.opened_ts)} to "
                f"{local_str(t.closed_ts)}",
                f"  Why it bought: {t.entry_reason}",
                f"  Why it sold: {t.exit_reason}",
            ]
            for n in t.news[:8]:
                scope = "market-wide" if n.market_wide else t.ticker
                lines.append(f"  News ({scope}, unverified): {n.title} [{', '.join(n.labels)}]")
    if lessons.still_held:
        lines += ["", f"Still held, so not scored yet: {', '.join(lessons.still_held)}"]
    return "\n".join(lines) + "\n"


def write_lessons_file(ctx: AppContext, path=None) -> str:
    """Rewrite the text file in one step, so a reader never sees half a report."""
    target = path or ctx.paths.lessons
    lessons = build_lessons(ctx.db.conn(), ctx.settings().news_lookback_hours)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(report_text(lessons, to_iso(ctx.clock()), ctx.demo), encoding="utf-8")
    os.replace(temp, target)
    return str(target)
