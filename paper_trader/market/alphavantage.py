"""Alpha Vantage (https://www.alphavantage.co/documentation/). Free key: about 25 calls/day.

The documentation lists SHOP.TRT (TSX) and GPV.TRV (TSX Venture) as symbol formats. On a
free key, prices update once, at the end of each trading day, so the app treats this as
an end-of-day source: one daily-history call per ticker per day supplies both the
history and the latest close.

These responses carry no currency field, so the currency comes from the listing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation

from ..http import fetch_json
from ..money import D
from ..symbols import currency_for, split_listing
from ..timeutil import to_iso
from .base import Bar, DataUnavailable, MarketDataProvider, Quote

BASE = "https://www.alphavantage.co/query"
DELAY_NOTE = "End of day: the last closing price, not a live price"
# 21:00 UTC is at or after the 16:00 Toronto/New York close all year.
CLOSE_UTC = timedelta(hours=21)


def provider_symbol(ticker: str) -> str:
    symbol, listing = split_listing(ticker)
    if listing == "NEO":
        raise DataUnavailable(
            "Alpha Vantage documents no symbol format for Cboe Canada. Try the .TO listing.")
    return {"TSX": f"{symbol}.TRT", "TSXV": f"{symbol}.TRV"}.get(listing, symbol)


def _check(data) -> dict:
    if not isinstance(data, dict):
        raise DataUnavailable("Unexpected response from Alpha Vantage.")
    for key in ("Error Message", "Note", "Information"):
        if key in data:
            raise DataUnavailable(f"Alpha Vantage: {str(data[key])[:160]}")
    return data


def _close_time(day: str) -> str:
    return to_iso(datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + CLOSE_UTC)


class AlphaVantage(MarketDataProvider):
    name = "Alpha Vantage"
    min_call_seconds = 13.0  # stays under 5 calls per minute
    end_of_day = True

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, **params) -> dict:
        return _check(fetch_json(BASE, {**params, "apikey": self.api_key}))

    def get_quote(self, ticker: str) -> Quote:
        data = self._get(function="GLOBAL_QUOTE", symbol=provider_symbol(ticker))
        return parse_quote(ticker, data, self.name)

    def get_daily_bars(self, ticker: str) -> list[Bar]:
        return parse_series(self._get(function="TIME_SERIES_DAILY", symbol=provider_symbol(ticker)))

    def quote_from_bars(self, ticker: str, bars: list[Bar]) -> Quote:
        return quote_from_bars(ticker, bars, self.name)


def quote_from_bars(ticker: str, bars: list[Bar], source: str = "Alpha Vantage") -> Quote:
    if not bars:
        raise DataUnavailable("Alpha Vantage returned no history for this symbol.")
    last = bars[-1]
    return Quote(ticker, last.close, currency_for(ticker), split_listing(ticker)[1],
                 _close_time(last.date), source, DELAY_NOTE, last.volume, end_of_day=True)


def parse_quote(ticker: str, data: dict, source: str = "Alpha Vantage") -> Quote:
    body = _check(data).get("Global Quote") or {}
    try:
        price = D(body["05. price"])
        quote_ts = _close_time(body["07. latest trading day"])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise DataUnavailable("Alpha Vantage returned no quote for this symbol.") from None
    volume = body.get("06. volume")
    return Quote(ticker, price, currency_for(ticker), split_listing(ticker)[1], quote_ts, source,
                 DELAY_NOTE, D(volume) if volume else None, end_of_day=True)


def parse_series(data: dict) -> list[Bar]:
    try:
        series = _check(data)["Time Series (Daily)"]
        bars = [Bar(day, D(v["4. close"]), D(v["5. volume"])) for day, v in series.items()]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise DataUnavailable("Alpha Vantage returned no history for this symbol.") from None
    return sorted(bars, key=lambda bar: bar.date)
