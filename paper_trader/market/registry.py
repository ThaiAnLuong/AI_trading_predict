"""Provider selection. Adding a provider means adding one entry here."""
from __future__ import annotations

from ..config import api_key
from ..settings import Settings
from ..timeutil import Clock
from .alphavantage import AlphaVantage
from .base import FxProvider, MarketDataProvider, NoProvider
from .demo import DemoFx, DemoMarket
from .fx import BankOfCanadaFx
from .twelvedata import TwelveData
from .yahoo import Yahoo

PROVIDERS = {  # name: (class, environment variable holding its key, or None if it needs none)
    "yahoo": (Yahoo, None),
    "alphavantage": (AlphaVantage, "ALPHAVANTAGE_API_KEY"),
    "twelvedata": (TwelveData, "TWELVEDATA_API_KEY"),
}


def build_market(s: Settings, demo: bool, clock: Clock) -> MarketDataProvider:
    if demo:
        return DemoMarket(clock)
    cls, env_name = PROVIDERS[s.market_provider]
    if env_name is None:
        return cls()
    key = api_key(env_name)
    if not key:
        return NoProvider(f"No market data key: set {env_name} in source API\\api_keys.env, "
                          "or run demo mode.")
    return cls(key)


def build_fx(demo: bool, clock: Clock) -> FxProvider:
    return DemoFx(clock) if demo else BankOfCanadaFx()
