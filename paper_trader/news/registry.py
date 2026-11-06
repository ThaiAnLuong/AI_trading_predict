"""Source types. Adding a type means adding a builder here; sources themselves are rows
in the news_sources table, managed from the News tab."""
from __future__ import annotations

from ..config import api_key
from ..market.base import DataUnavailable
from ..timeutil import Clock
from .base import NewsSource
from .demo import DemoNews
from .finnhub import FinnhubSource
from .rss import RssSource

USER_KINDS = ("rss", "finnhub")


def build_source(row, clock: Clock) -> NewsSource:
    kind = row["kind"]
    if kind == "rss":
        return RssSource(row["name"], row["url"])
    if kind == "finnhub":
        key = api_key("FINNHUB_API_KEY")
        if not key:
            raise DataUnavailable("FINNHUB_API_KEY is not set.")
        return FinnhubSource(row["name"], key, clock)
    if kind == "demo":
        return DemoNews(row["name"], clock)
    raise DataUnavailable(f"Unknown source type: {kind}")
