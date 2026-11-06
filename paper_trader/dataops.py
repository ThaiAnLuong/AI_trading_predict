"""Backups, CSV export and the experiment reset."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .ai.lessons import build_lessons, write_lessons_file
from .context import AppContext
from .money import D
from .performance import matched_series, max_drawdown_pct
from .settings import save_settings
from .timeutil import from_iso, local_date, local_midnight, local_str, to_iso

KEEP_AUTO_BACKUPS = 14
_RISKY_START = ("=", "+", "-", "@", "\t", "\r")


def _safe(value):
    """Neutralise spreadsheet formulas in text that came from outside (news titles etc.)."""
    if isinstance(value, str) and value.startswith(_RISKY_START):
        try:
            D(value)  # plain numbers such as -12.50 are left alone
            return value
        except Exception:
            return "'" + value
    return value


def write_csv(path: Path, headers: list[str], rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([_safe(v) for v in row])
    return path


def _dump(conn: sqlite3.Connection, query: str, path: Path, args=()) -> Path:
    """Write a query to CSV, adding a local-time column beside each stored UTC time."""
    cursor = conn.execute(query, args)
    names = [c[0] for c in cursor.description]
    position = names.index("ts") if "ts" in names else None
    headers = list(names)
    if position is not None:
        headers[position] = "ts_utc"
        headers.insert(position + 1, "ts_local")

    def rows():
        for row in cursor:
            values = list(row)
            if position is not None:
                values.insert(position + 1, local_str(row["ts"]))
            yield values

    return write_csv(path, headers, rows())


def trade_filter(account: str = "", ticker: str = "", side: str = "", date_from: str = "",
                 date_to: str = "") -> tuple[str, list]:
    """Build a WHERE clause for the trade log. Dates are local YYYY-MM-DD, inclusive."""
    clauses, args = [], []
    if account:
        clauses.append("account = ?")
        args.append(account)
    if ticker.strip():
        clauses.append("ticker = ?")
        args.append(ticker.strip().upper())
    if side:
        clauses.append("side = ?")
        args.append(side)
    for text, op, extra in ((date_from, ">=", 0), (date_to, "<", 1)):
        if text.strip():
            try:
                day = date.fromisoformat(text.strip())
            except ValueError:
                raise ValueError(f"'{text}' is not a date. Use YYYY-MM-DD.") from None
            clauses.append(f"ts {op} ?")
            args.append(to_iso(local_midnight(day + timedelta(days=extra))))
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), args


def export_trades(conn: sqlite3.Connection, path: Path, where: str = "", args=()) -> Path:
    return _dump(conn, f"SELECT * FROM trades {where} ORDER BY id", path, args)


def export_performance(conn: sqlite3.Connection, path: Path) -> Path:
    return _dump(conn, "SELECT * FROM snapshots ORDER BY ts, account", path)


def export_comparison(conn: sqlite3.Connection, path: Path) -> Path:
    rows = []
    my_values: list = []
    ai_values: list = []
    for p in matched_series(conn):
        my_values.append(D(p.my["total_value"]))
        ai_values.append(D(p.ai["total_value"]))
        rows.append((p.date, p.my["total_value"], p.ai["total_value"], p.my["cash"], p.ai["cash"],
                     p.my["realized"], p.ai["realized"], p.my["unrealized"], p.ai["unrealized"],
                     f"{max_drawdown_pct(my_values):.2f}", f"{max_drawdown_pct(ai_values):.2f}"))
    return write_csv(path, ["date", "my_total_cad", "ai_total_cad", "my_cash_cad", "ai_cash_cad",
                            "my_realized_cad", "ai_realized_cad", "my_unrealized_cad",
                            "ai_unrealized_cad", "my_max_drawdown_pct", "ai_max_drawdown_pct"],
                     rows)


def export_ai_results(ctx: AppContext, path: Path) -> Path:
    lessons = build_lessons(ctx.db.conn(), ctx.settings().news_lookback_hours)
    return write_csv(
        path, ["ticker", "opened_local", "closed_local", "days_held", "shares", "cost_cad",
               "proceeds_cad", "gain_cad", "gain_pct", "exit_kind", "why_bought", "why_sold",
               "news_labels", "news_keywords"],
        [(t.ticker, local_str(t.opened_ts), local_str(t.closed_ts), t.days_held, t.quantity, t.cost,
          t.proceeds, t.gain, f"{t.gain_pct:.2f}", t.exit_kind, t.entry_reason, t.exit_reason,
          "; ".join(sorted({x for n in t.news for x in n.labels})),
          "; ".join(sorted({x for n in t.news for x in n.keywords})))
         for t in lessons.finished])


def export_all(ctx: AppContext) -> Path:
    """Export every table of the experiment to a new timestamped folder."""
    conn = ctx.db.conn()
    stamp = ctx.clock().astimezone().strftime("%Y%m%d_%H%M%S")
    folder = ctx.paths.exports / f"export_{stamp}"
    export_trades(conn, folder / "trades.csv")
    export_performance(conn, folder / "performance_snapshots.csv")
    export_comparison(conn, folder / "comparison_daily.csv")
    _dump(conn, "SELECT * FROM ai_decisions ORDER BY id", folder / "ai_decisions.csv")
    _dump(conn, "SELECT * FROM ai_decision_news ORDER BY decision_id", folder / "ai_decision_news.csv")
    _dump(conn, "SELECT * FROM news_items ORDER BY published_ts", folder / "news_items.csv")
    _dump(conn, "SELECT * FROM settings_audit ORDER BY id", folder / "settings_changes.csv")
    _dump(conn, "SELECT * FROM watchlist ORDER BY ticker", folder / "watchlist.csv")
    export_ai_results(ctx, folder / "ai_trade_results.csv")
    write_lessons_file(ctx, folder / "ai_lessons.txt")
    settings = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT * FROM settings")}
    (folder / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return folder


def backup_now(ctx: AppContext, label: str = "manual") -> Path:
    stamp = ctx.clock().astimezone().strftime("%Y%m%d_%H%M%S")
    return ctx.db.backup_to(ctx.paths.backups / f"{ctx.paths.db.stem}_{label}_{stamp}.sqlite3")


def auto_backup(ctx: AppContext) -> Path | None:
    """At most one automatic backup a day, keeping the most recent few."""
    today = local_date(ctx.clock()).isoformat()
    if ctx.db.get_meta("last_auto_backup") == today:
        return None
    path = backup_now(ctx, "auto")
    ctx.db.set_meta("last_auto_backup", today)
    old = sorted(ctx.paths.backups.glob("*_auto_*.sqlite3"))
    for stale in old[:-KEEP_AUTO_BACKUPS]:
        stale.unlink(missing_ok=True)
    return path


def reset_experiment(ctx: AppContext, new_starting_balance: str | None = None) -> None:
    """Clear history and restart both accounts. Settings, watchlist and sources stay.

    The caller must have confirmed with the user and offered an export first.
    """
    now = to_iso(ctx.clock())
    with ctx.db.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('reset_in_progress', '1')")
        for table in ("trades", "snapshots", "ai_decisions", "ai_decision_news"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM meta WHERE key = 'reset_in_progress'")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('experiment_started_at', ?)",
                     (now,))
        conn.execute("INSERT INTO settings_audit(ts, key, old_value, new_value)"
                     " VALUES (?, 'experiment_reset', NULL, ?)", (now, new_starting_balance))
    if new_starting_balance:
        s = ctx.settings()
        s.starting_balance = new_starting_balance
        save_settings(ctx.db, s, ctx.clock)


def experiment_day(ctx: AppContext) -> tuple[int, datetime | None]:
    started = ctx.db.get_meta("experiment_started_at")
    if not started:
        return 0, None
    start = from_iso(started)
    return (ctx.clock() - start).days + 1, start + timedelta(days=152)
