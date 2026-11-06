"""Shared application objects, and first-run setup."""
from __future__ import annotations

from datetime import datetime

from .ai.llm_review import ClaudeReviewer
from .ai.strategy import MomentumNewsStrategy, Strategy
from .config import Paths
from .db import Database
from .engine import TradeEngine
from .market.base import FxProvider, MarketDataProvider
from .market.registry import build_fx, build_market
from .news.signals import seed_categories
from .settings import Settings, load_settings, save_settings
from .timeutil import to_iso, utc_now

CATEGORIES_VERSION = "2"
DEFAULT_NEWS_SOURCES = (
    # name, kind, url, enabled
    ("Bank of Canada press", "rss", "https://www.bankofcanada.ca/content_type/press/feed/", 1),
    ("Federal Reserve press", "rss", "https://www.federalreserve.gov/feeds/press_all.xml", 1),
    ("Finnhub company news", "finnhub", "", 0),
)


class AppClock:
    """Real time, except while demo history is being generated."""

    def __init__(self):
        self.override: datetime | None = None

    def __call__(self) -> datetime:
        return self.override or utc_now()


class AppContext:
    def __init__(self, paths: Paths, clock=None):
        self.paths = paths
        self.demo = paths.demo
        self.clock = clock or AppClock()
        self.db = Database(paths.db)
        self.engine = TradeEngine(self.db, self.clock, self.demo)
        self.strategy: Strategy = MomentumNewsStrategy()
        self._ensure_defaults()

    def settings(self) -> Settings:
        return load_settings(self.db)

    def market(self) -> MarketDataProvider:
        return build_market(self.settings(), self.demo, self.clock)

    def fx(self) -> FxProvider:
        return build_fx(self.demo, self.clock)

    def reviewer(self, s: Settings) -> ClaudeReviewer:
        return ClaudeReviewer(s.llm_model)

    def _ensure_defaults(self) -> None:
        # Once per version, so a category the user removed does not come back on restart.
        if self.db.get_meta("signal_categories_version") != CATEGORIES_VERSION:
            with self.db.tx() as conn:
                seed_categories(conn)
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                             ("signal_categories_version", CATEGORIES_VERSION))
        if self.db.get_meta("experiment_started_at"):
            return
        save_settings(self.db, load_settings(self.db), self.clock)
        with self.db.tx() as conn:
            sources = list(DEFAULT_NEWS_SOURCES)
            if self.demo:
                sources = [("DEMO Newswire (fake)", "demo", "", 1)]
            for source in sources:
                conn.execute(
                    "INSERT OR IGNORE INTO news_sources(name, kind, url, enabled) VALUES (?,?,?,?)",
                    source)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         ("experiment_started_at", to_iso(self.clock())))
