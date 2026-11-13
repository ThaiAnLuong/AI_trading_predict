"""SQLite storage: schema, per-thread connections, transactions and backups.

Decimals are stored as TEXT so no precision is lost. Timestamps are UTC ISO text.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings_audit (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, key TEXT NOT NULL, old_value TEXT, new_value TEXT);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    client_id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    account TEXT NOT NULL CHECK (account IN ('MY','AI')),
    source TEXT NOT NULL CHECK (source IN ('MANUAL','AI','IMPORT')),
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL','IMPORT')),
    sleeve TEXT NOT NULL CHECK (sleeve IN ('TRADING','IMPORTED')),
    ticker TEXT NOT NULL,
    exchange TEXT,
    currency TEXT NOT NULL CHECK (currency IN ('CAD','USD')),
    quantity TEXT NOT NULL,
    reference_price TEXT,
    fill_price TEXT NOT NULL,
    slippage_per_share TEXT NOT NULL DEFAULT '0',
    slippage_rule TEXT,
    fx_rate TEXT,
    fx_date TEXT,
    fx_source TEXT,
    gross_native TEXT NOT NULL,
    cash_delta TEXT NOT NULL,
    quote_ts TEXT,
    quote_source TEXT,
    quote_stale INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    decision_id INTEGER,
    -- The AI can only ever touch the AI account, and nothing else can touch it.
    CHECK ((account = 'AI' AND source = 'AI') OR (account = 'MY' AND source IN ('MANUAL','IMPORT'))),
    -- Imported holdings never move trading cash.
    CHECK (sleeve = 'TRADING' OR cash_delta = '0'),
    CHECK ((side = 'IMPORT') = (source = 'IMPORT')),
    CHECK (side <> 'IMPORT' OR sleeve = 'IMPORTED')
);
CREATE INDEX IF NOT EXISTS idx_trades_account_ts ON trades(account, ts);

CREATE TRIGGER IF NOT EXISTS trades_no_update BEFORE UPDATE ON trades
BEGIN SELECT RAISE(ABORT, 'trades are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trades_no_delete BEFORE DELETE ON trades
WHEN (SELECT value FROM meta WHERE key = 'reset_in_progress') IS NOT '1'
BEGIN SELECT RAISE(ABORT, 'trades are append-only'); END;

CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', added_ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS quotes (
    ticker TEXT PRIMARY KEY,
    price TEXT, currency TEXT, exchange TEXT,
    quote_ts TEXT, fetched_ts TEXT NOT NULL,
    source TEXT, delay_note TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok','unavailable')),
    error TEXT, volume TEXT, is_demo INTEGER NOT NULL DEFAULT 0,
    end_of_day INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS daily_bars (
    ticker TEXT NOT NULL, date TEXT NOT NULL, close TEXT NOT NULL, volume TEXT,
    source TEXT NOT NULL, PRIMARY KEY (ticker, date));

CREATE TABLE IF NOT EXISTS fx_rates (
    pair TEXT NOT NULL, rate_date TEXT NOT NULL, rate TEXT NOT NULL,
    fetched_ts TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (pair, rate_date));

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, local_date TEXT NOT NULL,
    account TEXT NOT NULL CHECK (account IN ('MY','AI')),
    cash TEXT NOT NULL, market_value TEXT NOT NULL, total_value TEXT NOT NULL,
    realized TEXT NOT NULL, unrealized TEXT NOT NULL, positions INTEGER NOT NULL,
    data_quality TEXT NOT NULL CHECK (data_quality IN ('complete','partial')),
    missing TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS idx_snapshots_account_ts ON snapshots(account, ts);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, cycle_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('BUY','SELL','HOLD')),
    quantity TEXT, reference_price TEXT, simulated_fill_price TEXT, currency TEXT,
    confidence TEXT, explanation TEXT NOT NULL, data_sources TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('EXECUTED','SUGGESTED','REJECTED','PAUSED','HOLD')),
    rejection_reason TEXT, strategy TEXT NOT NULL, trade_id INTEGER);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON ai_decisions(ts);

-- The AI's current plan for each holding it has (version 3). Rewritten at every cycle.
CREATE TABLE IF NOT EXISTS ai_plans (
    ticker TEXT PRIMARY KEY, ts TEXT NOT NULL, plan TEXT NOT NULL, target TEXT NOT NULL,
    stop TEXT NOT NULL, trailing INTEGER NOT NULL DEFAULT 0, add_at TEXT,
    why TEXT NOT NULL DEFAULT '', price TEXT, currency TEXT);

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL,
    published_ts TEXT NOT NULL, fetched_ts TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
    tickers TEXT NOT NULL DEFAULT '[]', labels TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_items(published_ts);

CREATE TABLE IF NOT EXISTS ai_decision_news (
    decision_id INTEGER NOT NULL, news_id INTEGER NOT NULL,
    PRIMARY KEY (decision_id, news_id));

CREATE TABLE IF NOT EXISTS news_sources (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
    last_status TEXT NOT NULL DEFAULT 'not fetched yet', last_fetch_ts TEXT);

CREATE TABLE IF NOT EXISTS signal_categories (
    name TEXT PRIMARY KEY, keywords TEXT NOT NULL,
    direction INTEGER NOT NULL DEFAULT 0 CHECK (direction IN (-1,0,1)),
    enabled INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS update_log (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
"""


class Database:
    """One SQLite file with a separate connection per thread."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        conn = self.conn()
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(quotes)")}
        if "end_of_day" not in columns:  # databases created by version 1
            conn.execute("ALTER TABLE quotes ADD COLUMN end_of_day INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self):
        """Write transaction. Takes the write lock up front so checks cannot race."""
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn().execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def backup_to(self, target: Path) -> Path:
        """Consistent online copy, verified before it is reported as good."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(target)
        try:
            self.conn().backup(dest)
            result = dest.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dest.close()
        if result != "ok":
            raise RuntimeError(f"Backup failed its integrity check: {result}")
        return target

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
