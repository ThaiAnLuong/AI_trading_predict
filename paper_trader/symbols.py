"""Ticker conventions used inside the app.

Canadian listings, priced in CAD, end in .TO (TSX), .V (TSX Venture) or .NE (Cboe
Canada, formerly NEO). This includes CDRs, the Canadian versions of big US companies,
such as GOOG.TO. Plain symbols (AAPL, BRK.B) are US listings priced in USD, and the
"CAD tickers only" setting blocks those. Providers translate these to their own
formats. A quote whose currency disagrees with this expectation is treated as
unavailable rather than silently mixed.
"""
from __future__ import annotations

import re

_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z]{1,2})?(?:\.(?:TO|V|NE))?$")
_LISTINGS = ((".TO", "TSX"), (".V", "TSXV"), (".NE", "NEO"))


def normalize_ticker(text: str) -> str:
    ticker = (text or "").strip().upper()
    if not ticker:
        raise ValueError("Enter a ticker symbol.")
    if not _TICKER.match(ticker):
        raise ValueError(
            f"'{ticker}' is not a valid ticker. Use RY.TO or GOOG.TO for TSX, ABC.V for "
            "TSX Venture, GOOG.NE for Cboe Canada, or AAPL for a US listing.")
    return ticker


def split_listing(ticker: str) -> tuple[str, str | None]:
    """Return (bare symbol, 'TSX' / 'TSXV' / 'NEO', or None for a US listing)."""
    for suffix, listing in _LISTINGS:
        if ticker.endswith(suffix):
            return ticker[:-len(suffix)], listing
    return ticker, None


def currency_for(ticker: str) -> str:
    return "CAD" if split_listing(ticker)[1] else "USD"


def cad_only_message(ticker: str) -> str:
    return (f"{ticker} is a US listing priced in USD, and this experiment is CAD only. Many big "
            f"US companies have a Canadian version, such as {ticker}.TO, though the symbol can "
            "differ (Visa is VISA.TO). To allow US tickers, turn off 'CAD tickers only' in "
            "Settings.")
