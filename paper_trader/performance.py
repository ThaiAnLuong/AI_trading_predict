"""Snapshots, period returns and the My-vs-AI comparison."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .db import Database
from .money import D, ZERO
from .settings import Settings, load_settings
from .timeutil import Clock, from_iso, local_date, to_iso
from .valuation import account_view

ACCOUNTS = ("MY", "AI")
ACCOUNT_LABELS = {"MY": "My account", "AI": "AI account"}
WINDOWS = (("Daily", 1), ("Weekly", 7), ("Monthly", 30), ("Yearly", 365))


def take_snapshots(db: Database, clock: Clock) -> None:
    """Record both accounts at the same instant, so comparisons line up."""
    now = clock()
    s = load_settings(db)
    with db.tx() as conn:
        for account in ACCOUNTS:
            view = account_view(conn, account, s, now)
            conn.execute(
                "INSERT INTO snapshots (ts, local_date, account, cash, market_value, total_value,"
                " realized, unrealized, positions, data_quality, missing)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (to_iso(now), local_date(now).isoformat(), account, str(view.cash),
                 str(view.market_value), str(view.total_value), str(view.realized),
                 str(view.unrealized), len(view.holdings), view.data_quality,
                 json.dumps(view.missing)))


def snapshot_due(conn: sqlite3.Connection, s: Settings, now: datetime) -> bool:
    row = conn.execute("SELECT MAX(ts) AS ts FROM snapshots").fetchone()
    if row["ts"] is None:
        return True
    return now - from_iso(row["ts"]) >= timedelta(minutes=s.snapshot_minutes)


def load_snapshots(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM snapshots WHERE account = ? ORDER BY ts, id",
                        (account,)).fetchall()


@dataclass(frozen=True)
class PeriodReturn:
    label: str
    pct: Decimal | None
    change: Decimal | None
    note: str


def period_returns(snapshots, current_total: Decimal, now: datetime) -> list[PeriodReturn]:
    """Change against the last snapshot at or before each cutoff.

    If history does not reach back that far the figure is n/a, not a guess.
    """
    results = []
    for label, days in WINDOWS:
        cutoff = now - timedelta(days=days)
        base = None
        for row in snapshots:
            if from_iso(row["ts"]) <= cutoff:
                base = row
            else:
                break
        if base is None:
            results.append(PeriodReturn(label, None, None, f"needs {days}+ day(s) of history"))
            continue
        start = D(base["total_value"])
        change = current_total - start
        results.append(PeriodReturn(label, change / start * 100, change,
                                    f"since {base['local_date']}"))
    return results


def max_drawdown_pct(values: list[Decimal]) -> Decimal:
    """Largest peak-to-trough fall, as a positive percentage."""
    peak, worst = None, ZERO
    for value in values:
        peak = value if peak is None or value > peak else peak
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


def daily_closes(snapshots) -> dict[str, sqlite3.Row]:
    """Last snapshot of each local date."""
    return {row["local_date"]: row for row in snapshots}


@dataclass(frozen=True)
class ComparisonPoint:
    date: str
    my: sqlite3.Row
    ai: sqlite3.Row


def matched_series(conn: sqlite3.Connection) -> list[ComparisonPoint]:
    """Daily points on dates where both accounts have a snapshot."""
    my = daily_closes(load_snapshots(conn, "MY"))
    ai = daily_closes(load_snapshots(conn, "AI"))
    return [ComparisonPoint(day, my[day], ai[day]) for day in sorted(my.keys() & ai.keys())]


@dataclass(frozen=True)
class ComparisonRow:
    account: str
    total_value: Decimal
    return_pct: Decimal
    realized: Decimal
    unrealized: Decimal
    drawdown_pct: Decimal
    trades: int
    cash: Decimal
    cash_pct: Decimal


def comparison(conn: sqlite3.Connection, s: Settings) -> list[ComparisonRow]:
    points = matched_series(conn)
    if not points:
        return []
    rows = []
    for account, pick in (("MY", lambda p: p.my), ("AI", lambda p: p.ai)):
        values = [D(pick(p)["total_value"]) for p in points]
        last = pick(points[-1])
        total = D(last["total_value"])
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE account = ? AND sleeve = 'TRADING'",
            (account,)).fetchone()["n"]
        rows.append(ComparisonRow(
            account, total, (total - s.start) / s.start * 100, D(last["realized"]),
            D(last["unrealized"]), max_drawdown_pct(values), count, D(last["cash"]),
            D(last["cash"]) / total * 100 if total else ZERO))
    return rows
