"""Yahoo Finance chart data, for personal use at your own choice.

UNOFFICIAL. Yahoo publishes no API for this, and its terms restrict automated
collection without permission, so it can stop working at any time. This code connects
directly and identifies itself honestly: no browser disguise, and no retry or workaround
if Yahoo refuses. A refused ticker simply shows as unavailable.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation

from ..http import fetch_json
from ..money import D, PRICE_STEP
from ..symbols import split_listing
from ..timeutil import to_iso
from .base import Bar, DataUnavailable, MarketDataProvider, Quote

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
NAME = "Yahoo Finance (unofficial)"
DELAY_NOTE = "Unofficial source; the delay is not stated, so check the price time"


def provider_symbol(ticker: str) -> str:
    """Yahoo writes share classes with a dash: BRK.B is BRK-B, CTC.A.TO is CTC-A.TO."""
    symbol, listing = split_listing(ticker)
    suffix = ticker[len(symbol):] if listing else ""
    return symbol.replace(".", "-") + suffix


def _price(value):
    return D(value).quantize(PRICE_STEP)


def _result(data) -> dict:
    try:
        chart = data["chart"]
        if chart.get("error"):
            raise DataUnavailable(f"Yahoo: {str(chart['error'].get('description'))[:160]}")
        return chart["result"][0]
    except (KeyError, IndexError, TypeError, AttributeError):
        raise DataUnavailable("Yahoo returned no data for this symbol.") from None


def parse_quote(ticker: str, data, source: str = NAME) -> Quote:
    try:
        meta = _result(data)["meta"]
        price = _price(meta["regularMarketPrice"])
        quote_ts = to_iso(datetime.fromtimestamp(int(meta["regularMarketTime"]), timezone.utc))
        currency = meta["currency"]
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
        raise DataUnavailable("Yahoo's quote was missing required fields.") from None
    volume = meta.get("regularMarketVolume")
    return Quote(ticker, price, currency, meta.get("fullExchangeName") or meta.get("exchangeName"),
                 quote_ts, source, DELAY_NOTE, D(volume) if volume is not None else None)


def parse_series(data) -> list[Bar]:
    try:
        result = _result(data)
        offset = timedelta(seconds=int(result["meta"].get("gmtoffset") or 0))
        quote = result["indicators"]["quote"][0]
        bars = []
        for stamp, close, volume in zip(result["timestamp"], quote["close"], quote["volume"]):
            if close is None:  # Yahoo leaves gaps as nulls; a gap is never filled in
                continue
            day = (datetime.fromtimestamp(int(stamp), timezone.utc) + offset).date().isoformat()
            bars.append(Bar(day, _price(close), D(volume) if volume is not None else None))
    except (KeyError, IndexError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
        raise DataUnavailable("Yahoo's history was missing required fields.") from None
    return bars


class Yahoo(MarketDataProvider):
    name = NAME
    min_call_seconds = 3.0  # stay gentle: a small watchlist, a few seconds apart

    def _chart(self, ticker: str, period: str):
        symbol = urllib.parse.quote(provider_symbol(ticker), safe="")
        return fetch_json(f"{BASE}/{symbol}", {"range": period, "interval": "1d"})

    def get_quote(self, ticker: str) -> Quote:
        return parse_quote(ticker, self._chart(ticker, "1d"), self.name)

    def get_daily_bars(self, ticker: str) -> list[Bar]:
        return parse_series(self._chart(ticker, "6mo"))
