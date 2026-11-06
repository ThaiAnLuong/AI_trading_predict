"""Market-data contract. To add a provider, subclass and register it in registry.py."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


class DataUnavailable(Exception):
    """No trustworthy data. The message is shown to the user (never contains a key)."""


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: Decimal
    currency: str
    exchange: str | None
    quote_ts: str          # when the provider says the price is from (UTC ISO)
    source: str
    delay_note: str
    volume: Decimal | None = None
    end_of_day: bool = False  # a closing price, not an intraday one


@dataclass(frozen=True)
class Bar:
    date: str  # YYYY-MM-DD
    close: Decimal
    volume: Decimal | None


@dataclass(frozen=True)
class FxQuote:
    rate: Decimal
    rate_date: str
    source: str


class MarketDataProvider(ABC):
    name = "base"
    is_demo = False
    min_call_seconds = 0.0  # spacing between calls, to respect free-tier limits
    end_of_day = False      # True: one history call per ticker per day gives the quote too

    @abstractmethod
    def get_quote(self, ticker: str) -> Quote:
        """Latest price, or raise DataUnavailable. Must never fabricate a value."""

    @abstractmethod
    def get_daily_bars(self, ticker: str) -> list[Bar]:
        """Recent daily closes, oldest first, or raise DataUnavailable."""

    def quote_from_bars(self, ticker: str, bars: list[Bar]) -> Quote:
        """For end-of-day providers: the latest close, as a quote."""
        raise NotImplementedError


class FxProvider(ABC):
    @abstractmethod
    def get_usd_cad(self) -> FxQuote:
        """Canadian dollars per US dollar, or raise DataUnavailable."""


class NoProvider(MarketDataProvider):
    """Used when no API key is configured: everything is honestly unavailable."""
    name = "NOT SET UP (add an API key to source API\\api_keys.env, see README)"

    def __init__(self, reason: str):
        self.reason = reason

    def get_quote(self, ticker: str) -> Quote:
        raise DataUnavailable(self.reason)

    def get_daily_bars(self, ticker: str) -> list[Bar]:
        raise DataUnavailable(self.reason)
