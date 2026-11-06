"""Finnhub company news (https://finnhub.io/docs/api/company-news).

Written from the published documentation and tested against a sample in that
format. It has not been run against the live service, because that needs a key.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from ..http import fetch_json
from ..market.base import DataUnavailable
from ..timeutil import Clock, local_date, to_iso
from .base import NewsItem, NewsSource

URL = "https://finnhub.io/api/v1/company-news"
MAX_ITEMS_PER_TICKER = 10


def parse_company_news(ticker: str, data, source_name: str) -> list[NewsItem]:
    if not isinstance(data, list):
        raise DataUnavailable("Unexpected response from Finnhub.")
    items = []
    for row in data[:MAX_ITEMS_PER_TICKER]:
        try:
            published = to_iso(datetime.fromtimestamp(int(row["datetime"]), timezone.utc))
            title, url = str(row["headline"]).strip(), str(row["url"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if title and url:
            publisher = str(row.get("source") or "").strip()
            name = f"{source_name}: {publisher}" if publisher else source_name
            items.append(NewsItem(title, name, published, url, str(row.get("summary") or "")[:400],
                                  (ticker,)))
    return items


class FinnhubSource(NewsSource):
    def __init__(self, name: str, api_key: str, clock: Clock):
        self.name = name
        self.api_key = api_key
        self.clock = clock

    def fetch(self, tickers: list[str]) -> list[NewsItem]:
        today = local_date(self.clock())
        params = {"from": (today - timedelta(days=3)).isoformat(), "to": today.isoformat(),
                  "token": self.api_key}
        items: list[NewsItem] = []
        errors = []
        for ticker in tickers:
            try:
                data = fetch_json(URL, {**params, "symbol": ticker})
                items.extend(parse_company_news(ticker, data, self.name))
            except DataUnavailable as exc:
                errors.append(f"{ticker}: {exc}")
            time.sleep(1.1)  # free tier is rate limited
        if errors and not items:
            raise DataUnavailable("; ".join(errors)[:300])
        return items
