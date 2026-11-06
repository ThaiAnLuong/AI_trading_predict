"""News contract. To add a source type, subclass and register it in registry.py."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published_ts: str  # UTC ISO
    url: str
    summary: str = ""
    tickers: tuple[str, ...] = field(default=())  # set when the source states relevance
    is_demo: bool = False


class NewsSource(ABC):
    @abstractmethod
    def fetch(self, tickers: list[str]) -> list[NewsItem]:
        """Return recent items, or raise DataUnavailable."""
