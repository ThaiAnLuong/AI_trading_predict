"""Turn stored news into labelled, unverified signals.

Keyword labels are a rough filter, not understanding. They can only adjust a decision
that price data already supports; they can never start a trade.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..symbols import split_listing
from ..timeutil import to_iso
from .base import NewsItem

DEFAULT_CATEGORIES = (
    # name, direction, enabled, keywords
    ("earnings_beat", 1, 1, ["beats estimates", "beat estimates", "tops estimates",
                             "beats expectations", "raises guidance", "record revenue"]),
    ("earnings_miss", -1, 1, ["misses estimates", "missed estimates", "misses expectations",
                              "cuts guidance", "lowers guidance", "profit warning"]),
    ("analyst_upgrade", 1, 1, ["upgraded", "upgrades", "raises price target"]),
    ("analyst_downgrade", -1, 1, ["downgraded", "downgrades", "cuts price target"]),
    ("company_announcement", 0, 1, ["announces", "acquisition", "merger", "dividend", "buyback"]),
    ("legal_regulatory", -1, 1, ["lawsuit", "investigation", "recall", "fraud"]),
    ("economic_release", 0, 1, ["cpi", "inflation", "gdp", "jobs report", "unemployment"]),
    ("interest_rates", 0, 1, ["interest rate", "policy rate", "rate cut", "rate hike", "fomc",
                              "federal funds"]),
    ("government_policy", 0, 1, ["tariff", "sanctions", "legislation", "federal budget"]),
    ("geopolitical", 0, 1, ["war", "invasion", "ceasefire", "conflict"]),
    ("politics", 0, 1, ["election", "parliament", "prime minister", "president", "congress",
                        "white house", "political"]),
    ("gold", 0, 1, ["gold", "bullion"]),
    ("supply_chain", 0, 1, ["supply chain", "shortage", "port closure", "strike action"]),
    ("commodities", 0, 1, ["oil price", "crude", "gold price", "copper", "natural gas"]),
    # Off by default: no documented social source ships with the app.
    ("social_media", 0, 0, ["trending", "viral", "reddit"]),
)


def seed_categories(conn: sqlite3.Connection) -> None:
    for name, direction, enabled, keywords in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO signal_categories(name, keywords, direction, enabled)"
            " VALUES (?, ?, ?, ?)", (name, json.dumps(keywords), direction, enabled))


def load_categories(conn: sqlite3.Connection) -> list[tuple[str, list[str], int]]:
    rows = conn.execute("SELECT * FROM signal_categories WHERE enabled = 1 ORDER BY name")
    return [(r["name"], json.loads(r["keywords"]), r["direction"]) for r in rows]


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE) is not None


def label_text(text: str, categories) -> tuple[list[str], list[str]]:
    """Return (event labels, matched keywords)."""
    labels, keywords = [], []
    for name, words, _ in categories:
        hits = [w for w in words if _contains(text, w)]
        if hits:
            labels.append(name)
            keywords.extend(hits)
    return labels, sorted(set(keywords))


def match_tickers(text: str, watchlist: dict[str, str]) -> list[str]:
    """Find watchlist tickers. Bare symbols are ignored because 'IT' or 'A' are ordinary
    words; only a company name or an explicit form like (TSX: RY) or $AAPL counts."""
    found = []
    for ticker, name in watchlist.items():
        symbol, _ = split_listing(ticker)
        explicit = re.search(
            rf"(?:\$|\b(?:TSX|TSXV|NYSE|NASDAQ):\s*){re.escape(symbol)}\b", text, re.IGNORECASE)
        if explicit or (name and _contains(text, name)):
            found.append(ticker)
    return found


def store_item(conn: sqlite3.Connection, item: NewsItem, categories, watchlist: dict[str, str],
               fetched_ts: str) -> None:
    text = f"{item.title}. {item.summary}"
    labels, keywords = label_text(text, categories)
    tickers = sorted(set(item.tickers) | set(match_tickers(text, watchlist)))
    conn.execute(
        "INSERT OR IGNORE INTO news_items (title, source, published_ts, fetched_ts, url, tickers,"
        " labels, keywords, summary, is_demo) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (item.title, item.source, item.published_ts, fetched_ts, item.url, json.dumps(tickers),
         json.dumps(labels), json.dumps(keywords), item.summary, int(item.is_demo)))


@dataclass(frozen=True)
class Signal:
    id: int
    title: str
    source: str
    published_ts: str
    url: str
    summary: str
    labels: tuple[str, ...]
    direction: int  # +1, 0 or -1, from the matched categories


def qualifying_signals(conn: sqlite3.Connection, ticker: str, now: datetime,
                       lookback_hours: int) -> list[Signal]:
    """Items usable as evidence: a source, a time inside the window, stated relevance to
    this ticker, and at least one event label."""
    since = to_iso(now - timedelta(hours=lookback_hours))
    rows = conn.execute(
        "SELECT * FROM news_items WHERE published_ts >= ? AND published_ts <= ? AND tickers LIKE ?"
        " ORDER BY published_ts DESC LIMIT 20",
        (since, to_iso(now), f'%"{ticker}"%')).fetchall()
    directions = {r["name"]: r["direction"]
                  for r in conn.execute("SELECT name, direction FROM signal_categories "
                                        "WHERE enabled = 1")}
    signals = []
    for row in rows:
        labels = tuple(x for x in json.loads(row["labels"]) if x in directions)
        if not (labels and row["source"] and row["url"]):
            continue
        votes = sum(directions[x] for x in labels)
        signals.append(Signal(row["id"], row["title"], row["source"], row["published_ts"],
                              row["url"], row["summary"], labels,
                              (votes > 0) - (votes < 0)))
    return signals
