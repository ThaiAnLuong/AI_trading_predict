"""Fake prices for demo mode. Deterministic random walks; not real market data."""
from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta

from ..money import D, PRICE_STEP
from ..symbols import currency_for, split_listing
from ..timeutil import Clock, local_date, to_iso
from .base import Bar, FxProvider, FxQuote, MarketDataProvider, Quote

SOURCE = "DEMO (fake data)"
DELAY_NOTE = "FAKE demo price, not market data"
ORIGIN = date(2025, 1, 1)


def _seed(*parts) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


class DemoMarket(MarketDataProvider):
    name = SOURCE
    is_demo = True

    def __init__(self, clock: Clock):
        self.clock = clock
        self._paths: dict[str, list[float]] = {}

    def _day_close(self, ticker: str, day: date) -> float:
        """Price on a day: a walk with a per-ticker drift, built from fixed daily steps."""
        rng = random.Random(_seed("base", ticker))
        start, drift = rng.uniform(40, 220), rng.uniform(-0.0004, 0.0012)
        path = self._paths.setdefault(ticker, [start])
        offset = (day - ORIGIN).days
        while len(path) <= offset:
            step = random.Random(_seed(ticker, len(path))).gauss(drift, 0.016)
            path.append(max(path[-1] * (1 + step), 1.0))
        return path[offset]

    def get_quote(self, ticker: str) -> Quote:
        now = self.clock()
        wiggle = random.Random(_seed(ticker, now.strftime("%Y%m%d%H%M"))).gauss(0, 0.002)
        price = self._day_close(ticker, local_date(now)) * (1 + wiggle)
        _, listing = split_listing(ticker)
        return Quote(ticker, D(f"{price:.4f}").quantize(PRICE_STEP), currency_for(ticker),
                     f"DEMO-{listing or 'US'}", to_iso(now), SOURCE, DELAY_NOTE, D(1_000_000))

    def get_daily_bars(self, ticker: str) -> list[Bar]:
        today = local_date(self.clock())
        days = [today - timedelta(days=n) for n in range(170, 0, -1)]
        return [Bar(d.isoformat(), D(f"{self._day_close(ticker, d):.4f}"),
                    D(random.Random(_seed("v", ticker, d)).randint(600_000, 1_800_000)))
                for d in days if d.weekday() < 5]


class DemoFx(FxProvider):
    def __init__(self, clock: Clock):
        self.clock = clock

    def get_usd_cad(self) -> FxQuote:
        day = local_date(self.clock())
        rate = 1.38 + random.Random(_seed("fx", day)).uniform(-0.02, 0.02)
        return FxQuote(D(f"{rate:.4f}"), day.isoformat(), "DEMO fake FX rate")
