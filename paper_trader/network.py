"""Market links: how an event in one area tends to move stocks in another.

Each link reads recent headlines. When one matches, it gives a +1 or -1 nudge to the
sectors it names. A nudge works like any other news signal: it can veto an AI buy or
raise its confidence, but it can never start a trade on its own. The links are
well-known tendencies, not tested rules; the AI lessons log is where they get checked.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .news.signals import Signal
from .symbols import split_listing
from .timeutil import to_iso

UNCLASSIFIED = "Unclassified"
COMPANY = "COMPANY"  # a link aimed at the company named in the headline

SECTORS = (
    "Oil & gas producers", "Pipelines & midstream", "Airlines & travel", "Banks",
    "Asset managers & exchanges", "Insurance & conglomerates", "Payments", "Telecom",
    "Utilities", "Consumer staples", "Restaurants", "Retail & apparel", "Media & entertainment",
    "Autos & EV", "Big tech & cloud", "Software", "AI chips & semiconductors",
    "Memory & storage", "Hardware & servers", "Consumer electronics",
    "Cybersecurity & networking", "Crypto", "Industrials", "Healthcare", "Broad market ETF",
    "Gold & silver miners", "Base metals miners", "Defence", "Fertilizer & chemicals",
    "Real estate", UNCLASSIFIED,
)

# By symbol, so GOOG.TO and GOOG.NE share a sector. Change any of these on the Watchlist tab.
DEFAULT_SECTORS = {
    "CNQ": "Oil & gas producers", "SU": "Oil & gas producers", "CVE": "Oil & gas producers",
    "WCP": "Oil & gas producers", "OXY": "Oil & gas producers", "IMO": "Oil & gas producers",
    "TOU": "Oil & gas producers", "XOM": "Oil & gas producers", "CVX": "Oil & gas producers",
    "ENB": "Pipelines & midstream", "TRP": "Pipelines & midstream", "PPL": "Pipelines & midstream",
    "AC": "Airlines & travel", "WJA": "Airlines & travel",
    "RY": "Banks", "TD": "Banks", "BNS": "Banks", "BMO": "Banks", "CM": "Banks", "NA": "Banks",
    "JPM": "Banks", "BAC": "Banks", "WFC": "Banks", "GS": "Banks",
    "BLK": "Asset managers & exchanges", "SPGI": "Asset managers & exchanges",
    "X": "Asset managers & exchanges", "BN": "Asset managers & exchanges",
    "BRK": "Insurance & conglomerates", "MFC": "Insurance & conglomerates",
    "SLF": "Insurance & conglomerates", "FFH": "Insurance & conglomerates",
    "VISA": "Payments", "V": "Payments", "MA": "Payments", "FISV": "Payments", "PYPL": "Payments",
    "BCE": "Telecom", "T": "Telecom", "VZ": "Telecom", "RCI": "Telecom",
    "FTS": "Utilities", "EMA": "Utilities", "H": "Utilities",
    "PEP": "Consumer staples", "COLA": "Consumer staples", "KO": "Consumer staples",
    "WMT": "Consumer staples", "STZ": "Consumer staples", "COST": "Consumer staples",
    "L": "Consumer staples", "ATD": "Consumer staples", "DOL": "Consumer staples",
    "MCD": "Restaurants", "QSR": "Restaurants", "SBUX": "Restaurants",
    "NKE": "Retail & apparel", "HD": "Retail & apparel", "CTC": "Retail & apparel",
    "LULU": "Retail & apparel",
    "DIS": "Media & entertainment", "NFLX": "Media & entertainment",
    "TSLA": "Autos & EV", "GM": "Autos & EV", "F": "Autos & EV", "MG": "Autos & EV",
    "MSFT": "Big tech & cloud", "GOOG": "Big tech & cloud", "GOOGL": "Big tech & cloud",
    "AMZN": "Big tech & cloud", "META": "Big tech & cloud", "ORAC": "Big tech & cloud",
    "ORCL": "Big tech & cloud",
    "ADBE": "Software", "SHOP": "Software", "CRM": "Software", "CSU": "Software",
    "OTEX": "Software", "DSG": "Software",
    "NVDA": "AI chips & semiconductors", "AMD": "AI chips & semiconductors",
    "AVGO": "AI chips & semiconductors", "QCOM": "AI chips & semiconductors",
    "INTC": "AI chips & semiconductors", "ASML": "AI chips & semiconductors",
    "TSM": "AI chips & semiconductors",
    "WDC": "Memory & storage", "SNDK": "Memory & storage", "MU": "Memory & storage",
    "DELL": "Hardware & servers", "SMCI": "Hardware & servers", "CLS": "Hardware & servers",
    "AAPL": "Consumer electronics",
    "FTNT": "Cybersecurity & networking", "CSCO": "Cybersecurity & networking",
    "BB": "Cybersecurity & networking", "CRWD": "Cybersecurity & networking",
    "PANW": "Cybersecurity & networking",
    "COIN": "Crypto", "BTCY": "Crypto", "ETHY": "Crypto", "BTCC": "Crypto", "ETHX": "Crypto",
    "CATR": "Industrials", "CAT": "Industrials", "MMMM": "Industrials", "MMM": "Industrials",
    "CP": "Industrials", "CNR": "Industrials", "WCN": "Industrials",
    "JNJ": "Healthcare", "SYK": "Healthcare", "LLY": "Healthcare", "NVO": "Healthcare",
    "PFE": "Healthcare", "UNH": "Healthcare",
    "VFV": "Broad market ETF", "XIU": "Broad market ETF", "ZCN": "Broad market ETF",
    "XIC": "Broad market ETF", "ZSP": "Broad market ETF", "VCN": "Broad market ETF",
    "ABX": "Gold & silver miners", "AEM": "Gold & silver miners", "K": "Gold & silver miners",
    "WPM": "Gold & silver miners", "FNV": "Gold & silver miners", "PAAS": "Gold & silver miners",
    "TECK": "Base metals miners", "FM": "Base metals miners", "IVN": "Base metals miners",
    "CCO": "Base metals miners",
    "CAE": "Defence", "LMT": "Defence", "RTX": "Defence",
    "NTR": "Fertilizer & chemicals",
    # Funds that follow one stock or one group: filed under what they track. Check these.
    "NVHE": "AI chips & semiconductors", "SHPY": "Software", "TSLY": "Autos & EV",
    "DFN": "Banks", "DS": "Broad market ETF",
    "REI": "Real estate", "CAR": "Real estate", "GRT": "Real estate",
}

# Words that say which way a price moved. Used only by links marked `moves`.
UP_WORDS = ("rise", "rises", "rising", "rose", "jump", "jumps", "jumped", "surge", "surges",
            "surged", "soar", "soars", "soared", "climb", "climbs", "climbed", "rally", "rallies",
            "rallied", "spike", "spikes", "spiked", "gain", "gains", "gained", "higher", "highs",
            "record high", "up", "strong", "stronger", "beats", "boost", "boosts", "increase",
            "increases", "expands")
DOWN_WORDS = ("fall", "falls", "falling", "fell", "drop", "drops", "dropped", "plunge",
              "plunges", "plunged", "slump", "slumps", "slumped", "slide", "slides", "slid",
              "tumble", "tumbles", "tumbled", "sink", "sinks", "sank", "lower", "lows", "down",
              "decline", "declines", "declined", "sell-off", "selloff", "weak", "weaker",
              "misses", "slashes", "cuts", "retreat", "retreats")


@dataclass(frozen=True)
class Link:
    id: int
    name: str              # "Oil price" -> read with the arrow: "Oil price ↑"
    any_of: tuple          # the headline must contain one of these
    up: tuple = ()         # sectors that tend to rise when the event happens (or the price rises)
    down: tuple = ()       # sectors that tend to fall
    need: tuple = ()       # and also one of these, when given
    exclude: tuple = ()    # never match a headline containing one of these
    moves: bool = False    # True: a price that can go either way; the headline's verbs decide
    note: str = ""

    def effect(self, sector: str) -> int:
        return (sector in self.up) - (sector in self.down)

    def title(self) -> str:
        return f"{self.name} {'↑/↓' if self.moves else ''}".strip()


OIL = "Oil & gas producers"
LINKS = (
    Link(1, "Oil price", ("oil price", "oil prices", "crude", "brent", "wti", "oil futures"),
         up=(OIL,), down=("Airlines & travel", "Fertilizer & chemicals"), moves=True,
         exclude=("palm oil", "olive oil", "cooking oil")),
    Link(2, "Oil price and the Canadian dollar", ("oil price", "crude"), moves=True,
         note="Information only: CDRs are currency-hedged, so the loonie barely moves them."),
    Link(3, "Natural gas price", ("natural gas", "lng"),
         up=(OIL, "Pipelines & midstream"), down=("Fertilizer & chemicals", "Utilities"),
         moves=True, exclude=("gasoline",)),
    Link(4, "OPEC output cut", ("opec", "opec+"), need=("cut", "cuts", "output cut",
                                                         "production cut", "reduce output"),
         up=(OIL,), down=("Airlines & travel",)),
    Link(5, "Fuel prices", ("gasoline", "gas prices", "fuel prices", "diesel", "jet fuel", "pump prices"),
         down=("Consumer staples", "Restaurants", "Airlines & travel"), moves=True),
    Link(6, "Copper price", ("copper",), up=("Base metals miners", "Industrials"), moves=True),
    Link(7, "War or conflict starts", ("war", "invasion", "invades", "airstrike", "airstrikes",
                                       "missile strike", "military strike", "troops"),
         up=("Gold & silver miners", "Defence", OIL), down=("Airlines & travel", "Broad market ETF"),
         exclude=("trade war", "price war", "star wars", "culture war", "war chest",
                  "bidding war", "talent war", "ceasefire")),
    Link(8, "War in an oil region", ("iran", "iraq", "saudi", "hormuz", "red sea", "middle east",
                                     "israel", "houthi", "russia", "libya"),
         need=("war", "attack", "attacks", "strike", "strikes", "missile", "drone", "conflict",
               "invasion", "blockade"),
         up=(OIL, "Pipelines & midstream"), down=("Airlines & travel",),
         exclude=("ceasefire", "peace")),
    Link(9, "Market panic", ("sell-off", "selloff", "market crash", "stocks plunge", "vix",
                             "fear gauge", "panic selling", "rout"),
         up=("Gold & silver miners",),
         down=("AI chips & semiconductors", "Hardware & servers", "Crypto", "Software")),
    Link(10, "Peace deal or ceasefire", ("ceasefire", "peace deal", "peace agreement", "truce",
                                         "peace talks"),
         up=("Airlines & travel", "Broad market ETF"),
         down=("Gold & silver miners", "Defence", OIL)),
    Link(11, "Central bank raises rates", ("rate hike", "rate hikes", "raises rates",
                                           "raises interest rates", "hikes rates",
                                           "raises its policy rate", "raised rates"),
         up=("Banks",), down=("Software", "Big tech & cloud", "Real estate", "Utilities", "Telecom")),
    Link(12, "Central bank cuts rates", ("rate cut", "rate cuts", "cuts rates",
                                         "cuts interest rates", "lowers rates", "cut rates",
                                         "cuts its policy rate", "lowered rates"),
         up=("Software", "Big tech & cloud", "Real estate", "Gold & silver miners",
             "Broad market ETF"),
         down=("Banks",)),
    Link(13, "Inflation hotter than expected", ("inflation", "cpi", "consumer prices"),
         need=("hotter", "higher than expected", "above expectations", "accelerates",
               "accelerated", "surges", "jumps", "sticky"),
         up=("Gold & silver miners",), down=("Software", "Big tech & cloud", "Broad market ETF")),
    Link(14, "US dollar", ("us dollar", "u.s. dollar", "greenback", "dollar index"),
         down=("Gold & silver miners", "Base metals miners"), moves=True,
         note="A stronger US dollar usually weighs on gold and metals."),
    Link(15, "Canadian dollar (loonie)", ("canadian dollar", "loonie"),
         up=("Airlines & travel",), down=(OIL,), moves=True,
         note="A strong loonie cuts exporters' CAD revenue and lowers airlines' USD costs."),
    Link(16, "Recession fears", ("recession", "economic slowdown", "layoffs", "job cuts",
                                 "economy contracts", "contraction"),
         up=("Consumer staples", "Gold & silver miners", "Utilities"),
         down=("Banks", OIL, "Airlines & travel", "Retail & apparel", "Industrials")),
    Link(17, "Major new AI model launched", ("ai model", "new model", "gpt", "gemini", "claude",
                                             "llm", "chatbot", "frontier model"),
         need=("launch", "launches", "launched", "releases", "released", "unveils", "unveiled",
               "introduces", "debuts"),
         up=("AI chips & semiconductors", "Big tech & cloud"), down=("Software",)),
    Link(18, "AI and data-centre spending", ("capex", "capital spending", "data center",
                                             "data centre", "data centers", "data centres",
                                             "ai spending", "ai infrastructure"),
         up=("AI chips & semiconductors", "Hardware & servers", "Memory & storage"), moves=True),
    Link(19, "Chip export limits on China", ("export controls", "export restrictions",
                                             "export ban", "chip ban", "entity list"),
         down=("AI chips & semiconductors",)),
    Link(20, "Memory chip shortage", ("memory", "dram", "nand", "hbm"),
         need=("shortage", "price hike", "prices rise", "tight supply", "prices surge"),
         up=("Memory & storage",), down=("Hardware & servers", "Consumer electronics")),
    Link(21, "Major cyberattack or outage", ("cyberattack", "cyber attack", "ransomware",
                                            "data breach", "hacked", "outage"),
         up=("Cybersecurity & networking",)),
    Link(22, "Big product launch", ("unveils", "launches", "new iphone", "product launch"),
         up=(COMPANY,), note="Applies to the company named in the headline."),
    Link(23, "Bitcoin and crypto prices", ("bitcoin", "crypto", "ether", "ethereum",
                                           "cryptocurrency"), up=("Crypto",), moves=True),
    Link(24, "Crypto crackdown", ("crypto", "bitcoin", "cryptocurrency", "stablecoin"),
         need=("crackdown", "ban", "bans", "sues", "lawsuit", "charges", "charged"),
         down=("Crypto",)),
    Link(25, "Scandal, probe, lawsuit or recall", ("scandal", "fraud", "probe", "lawsuit",
                                                   "recall", "recalls", "investigation", "sued",
                                                   "charged"),
         down=(COMPANY,), note="Applies to the company named in the headline."),
    Link(26, "Earnings beat", ("beats estimates", "beat estimates", "tops estimates",
                               "beats expectations", "raises guidance", "record revenue",
                               "record profit"),
         up=(COMPANY,), note="Applies to the company named in the headline."),
    Link(27, "Tariffs or trade war", ("tariff", "tariffs", "trade war"),
         down=("Retail & apparel", "Consumer electronics", "Industrials", "Autos & EV")),
    Link(28, "Weight-loss drug boom (GLP-1)", ("glp-1", "ozempic", "wegovy", "zepbound",
                                               "mounjaro", "weight-loss drug"),
         up=("Healthcare",), down=("Consumer staples", "Restaurants")),
    Link(29, "Jobs report", ("jobs report", "payrolls", "job gains", "hiring", "employment"),
         up=("Banks", "Retail & apparel"), moves=True),
    Link(30, "Election or political change", ("election",),
         note="Information only: the effect depends on the winner's policies."),
)


# -- sectors ------------------------------------------------------------------------

def default_sector(ticker: str) -> str:
    symbol, _ = split_listing(ticker)
    return DEFAULT_SECTORS.get(symbol.upper().replace(".", "-").split("-")[0], UNCLASSIFIED)


def sector_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Your choices from the Watchlist tab. Tickers not listed use default_sector."""
    return {r["ticker"]: r["sector"] for r in conn.execute("SELECT * FROM ticker_sectors")}


def sector_for(ticker: str, chosen: dict[str, str]) -> str:
    return chosen.get(ticker) or default_sector(ticker)


def set_sector(conn: sqlite3.Connection, ticker: str, sector: str) -> None:
    if sector not in SECTORS:
        raise ValueError(f"Unknown sector: {sector}")
    conn.execute("INSERT OR REPLACE INTO ticker_sectors(ticker, sector) VALUES (?, ?)",
                 (ticker, sector))


# -- matching -----------------------------------------------------------------------

def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", text, re.IGNORECASE) is not None


def _direction(text: str) -> int:
    up = any(_has(text, w) for w in UP_WORDS)
    down = any(_has(text, w) for w in DOWN_WORDS)
    return up - down  # both or neither: 0, the headline is unclear


def link_direction(link: Link, text: str) -> int:
    """+1 if the event happened (or the price rose), -1 if the price fell, 0 for no match."""
    if any(_has(text, x) for x in link.exclude):
        return 0
    if not any(_has(text, x) for x in link.any_of):
        return 0
    if link.need and not any(_has(text, x) for x in link.need):
        return 0
    return _direction(text) if link.moves else 1


@dataclass(frozen=True)
class Hit:
    link: Link
    direction: int  # +1 or -1: which way the event went
    news_id: int
    title: str
    source: str
    published_ts: str
    url: str
    summary: str
    tickers: tuple   # companies named in the headline


def recent_hits(conn: sqlite3.Connection, now: datetime, lookback_hours: int,
                links=LINKS) -> list[Hit]:
    """Every link that matched a headline inside the window, newest first."""
    rows = conn.execute(
        "SELECT * FROM news_items WHERE published_ts >= ? AND published_ts <= ?"
        " ORDER BY published_ts DESC LIMIT 2000",
        (to_iso(now - timedelta(hours=lookback_hours)), to_iso(now))).fetchall()
    hits = []
    for row in rows:
        if not (row["source"] and row["url"]):
            continue
        text = row["title"]  # headlines only: summaries matched too loosely in testing
        for link in links:
            d = link_direction(link, text)
            if d:
                hits.append(Hit(link, d, row["id"], row["title"], row["source"],
                                row["published_ts"], row["url"], row["summary"],
                                tuple(json.loads(row["tickers"]))))
    return hits


def effect_on(hit: Hit, ticker: str, sector: str) -> int:
    """+1, -1 or 0: what this matched headline means for one stock."""
    if COMPANY in hit.link.up or COMPANY in hit.link.down:
        if ticker not in hit.tickers:
            return 0
        return hit.direction * ((COMPANY in hit.link.up) - (COMPANY in hit.link.down))
    return hit.direction * hit.link.effect(sector)


def net_effect(hits: list[Hit], ticker: str, sector: str) -> tuple[int, list[tuple[Hit, int]]]:
    """Net push on a stock (one vote per link, so 20 oil headlines count once) and the
    headlines behind it."""
    per_link: dict[int, int] = {}
    used = []
    for hit in hits:
        e = effect_on(hit, ticker, sector)
        if e:
            used.append((hit, e))
            per_link.setdefault(hit.link.id, e)  # newest headline per link decides
    return sum(per_link.values()), used


def link_signals(hits: list[Hit], ticker: str, sector: str) -> list[Signal]:
    """Signals for the AI: only the side that wins the net vote, one per link."""
    net, used = net_effect(hits, ticker, sector)
    if net == 0:
        return []
    want = 1 if net > 0 else -1
    out, seen = [], set()
    for hit, e in used:
        if e != want or hit.link.id in seen:
            continue
        seen.add(hit.link.id)
        arrow = "" if not hit.link.moves else (" ↑" if hit.direction > 0 else " ↓")
        out.append(Signal(hit.news_id, hit.title, hit.source, hit.published_ts, hit.url,
                          hit.summary, (f"link {hit.link.id}: {hit.link.name}{arrow}",), e))
    return out
