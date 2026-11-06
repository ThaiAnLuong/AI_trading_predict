"""Made-up headlines for demo mode. Every item is labelled as fake."""
from __future__ import annotations

import random

from ..market.demo import _seed
from ..timeutil import Clock, local_date, to_iso
from .base import NewsItem, NewsSource

HEADLINES = (
    "[DEMO] {t} beats estimates in fictional quarter",
    "[DEMO] {t} upgraded by imaginary analyst",
    "[DEMO] {t} misses estimates in fictional quarter",
    "[DEMO] {t} downgraded by imaginary analyst",
    "[DEMO] {t} announces fictional product",
)


class DemoNews(NewsSource):
    def __init__(self, name: str, clock: Clock):
        self.name = name
        self.clock = clock

    def fetch(self, tickers: list[str]) -> list[NewsItem]:
        now = self.clock()
        day = local_date(now)
        items = []
        for ticker in tickers:
            rng = random.Random(_seed("news", ticker, day))
            if rng.random() < 0.2:
                title = rng.choice(HEADLINES).format(t=ticker)
                url = f"https://example.invalid/demo/{ticker}/{day}"
                items.append(NewsItem(title, self.name, to_iso(now), url,
                                      "FAKE demo item. Nothing here happened.", (ticker,), True))
        return items
