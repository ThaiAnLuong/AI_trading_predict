"""Shared test setup: a throwaway database with a controllable clock."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from paper_trader.db import Database
from paper_trader.engine import TradeEngine, TradeRequest
from paper_trader.settings import Settings, load_settings, save_settings
from paper_trader.symbols import currency_for
from paper_trader.timeutil import to_iso


class FakeClock:
    def __init__(self, start: datetime | None = None):
        # Wednesday, in local time, well away from any midnight.
        self.now = start or datetime(2026, 11, 4, 12, 0, 0).astimezone()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class DbTestCase(unittest.TestCase):
    demo = True

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="paper_trader_test_"))
        self.db = Database(self.dir / "test.sqlite3")
        self.clock = FakeClock()
        self.engine = TradeEngine(self.db, self.clock, demo=self.demo)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def allow_usd(self) -> None:
        self.settings(cad_only=False)

    def settings(self, **changes) -> Settings:
        s = load_settings(self.db)
        for key, value in changes.items():
            setattr(s, key, value)
        save_settings(self.db, s, self.clock)
        return s

    def quote(self, ticker: str, price, *, currency: str | None = None, status: str = "ok",
              age_minutes: float = 0, is_demo: bool | None = None, error: str | None = None):
        self.db.conn().execute(
            "INSERT OR REPLACE INTO quotes (ticker, price, currency, exchange, quote_ts, fetched_ts,"
            " source, status, error, is_demo) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ticker, None if price is None else str(price), currency or currency_for(ticker), "TEST",
             to_iso(self.clock() - timedelta(minutes=age_minutes)), to_iso(self.clock()), "test",
             status, error, int(self.demo if is_demo is None else is_demo)))

    def fx(self, rate="1.40", rate_date: str | None = None):
        day = rate_date or self.clock().astimezone().date().isoformat()
        self.db.conn().execute(
            "INSERT OR REPLACE INTO fx_rates (pair, rate_date, rate, fetched_ts, source)"
            " VALUES ('USDCAD', ?, ?, ?, 'test')", (day, rate, to_iso(self.clock())))

    def buy(self, ticker="RY.TO", qty=1, account="MY", **kwargs) -> int:
        return self.engine.submit(self.req("BUY", ticker, qty, account, **kwargs))

    def sell(self, ticker="RY.TO", qty=1, account="MY", **kwargs) -> int:
        return self.engine.submit(self.req("SELL", ticker, qty, account, **kwargs))

    def req(self, side, ticker, qty, account="MY", **kwargs) -> TradeRequest:
        source = "AI" if account == "AI" else "MANUAL"
        kwargs.setdefault("reason", "test")
        kwargs.setdefault("confirm_duplicate", True)
        return TradeRequest(account, source, side, ticker, qty, **kwargs)
