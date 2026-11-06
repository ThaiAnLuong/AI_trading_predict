"""Twelve Data (https://twelvedata.com/docs). Free Basic plan: 8 credits/min, 800/day.

The free plan lists US equities only. Canadian listings come through the paid Cboe
Canada add-on. If a plan refuses a symbol, the ticker simply shows as unavailable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import InvalidOperation

from ..http import fetch_json
from ..money import D
from ..symbols import split_listing
from ..timeutil import to_iso
from .base import Bar, DataUnavailable, MarketDataProvider, Quote

BASE = "https://api.twelvedata.com"
DELAY_NOTE = "Timing depends on your Twelve Data plan and the exchange; see the quote time"


def provider_symbol(ticker: str) -> str:
    symbol, listing = split_listing(ticker)
    return f"{symbol}:{listing}" if listing else symbol


class TwelveData(MarketDataProvider):
    name = "Twelve Data"
    min_call_seconds = 8.0  # stays under 8 calls per minute

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, path: str, **params) -> dict:
        data = fetch_json(f"{BASE}/{path}", {**params, "apikey": self.api_key})
        if not isinstance(data, dict):
            raise DataUnavailable("Unexpected response from Twelve Data.")
        if data.get("status") == "error" or "code" in data:
            raise DataUnavailable(str(data.get("message", "Twelve Data returned an error."))[:200])
        return data

    def get_quote(self, ticker: str) -> Quote:
        return parse_quote(ticker, self._get("quote", symbol=provider_symbol(ticker)), self.name)

    def get_daily_bars(self, ticker: str) -> list[Bar]:
        data = self._get("time_series", symbol=provider_symbol(ticker), interval="1day",
                         outputsize=100)
        return parse_series(data)


def parse_quote(ticker: str, data: dict, source: str = "Twelve Data") -> Quote:
    try:
        price = D(data["close"])
        stamp = data.get("last_quote_at") or data["timestamp"]
        quote_ts = to_iso(datetime.fromtimestamp(int(stamp), timezone.utc))
        currency = data["currency"]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise DataUnavailable("Twelve Data quote was missing required fields.") from None
    volume = data.get("volume")
    return Quote(ticker, price, currency, data.get("exchange"), quote_ts, source, DELAY_NOTE,
                 D(volume) if volume else None)


def parse_series(data: dict) -> list[Bar]:
    try:
        bars = [Bar(v["datetime"][:10], D(v["close"]), D(v["volume"]) if v.get("volume") else None)
                for v in data["values"]]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise DataUnavailable("Twelve Data history was missing required fields.") from None
    return sorted(bars, key=lambda bar: bar.date)
