"""Mark an account to market using stored quotes. Never invents a price.

A holding with no usable price is shown as n/a and carried at book cost, and the
account is flagged as partial, so nothing looks more precise than it is.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from .ledger import AccountState, Position, period_used, replay
from .money import D, ZERO
from .settings import Settings
from .symbols import currency_for
from .timeutil import age_minutes, local_date, next_reset, period_start

TRADABLE_STATUS = ("ok", "end of day")


@dataclass(frozen=True)
class FxRate:
    rate: Decimal
    rate_date: str
    source: str


@dataclass
class HoldingView:
    ticker: str
    currency: str
    quantity: Decimal
    avg_cost: Decimal
    book_cost: Decimal
    price: Decimal | None
    exchange: str | None
    quote_ts: str | None
    status: str  # live-enough label for the table
    market_value: Decimal | None  # CAD
    unrealized: Decimal | None    # CAD
    fx_rate: Decimal | None


@dataclass
class AccountView:
    account: str
    starting: Decimal
    state: AccountState
    holdings: list[HoldingView] = field(default_factory=list)
    market_value: Decimal = ZERO
    unrealized: Decimal = ZERO
    missing: list[str] = field(default_factory=list)

    @property
    def cash(self) -> Decimal:
        return self.state.cash

    @property
    def realized(self) -> Decimal:
        return self.state.realized

    @property
    def total_value(self) -> Decimal:
        return self.cash + self.market_value

    @property
    def total_return_pct(self) -> Decimal:
        return (self.total_value - self.starting) / self.starting * 100

    @property
    def data_quality(self) -> str:
        return "partial" if self.missing else "complete"


@dataclass(frozen=True)
class Budget:
    limit: Decimal
    used: Decimal
    remaining: Decimal
    buying_power: Decimal
    period: str
    resets_at: datetime


def load_trades(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM trades WHERE account = ? ORDER BY id", (account,)).fetchall()


def load_state(conn: sqlite3.Connection, account: str, s: Settings) -> AccountState:
    return replay(s.start, load_trades(conn, account))


def load_quotes(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["ticker"]: row for row in conn.execute("SELECT * FROM quotes")}


def latest_fx(conn: sqlite3.Connection, s: Settings, now: datetime) -> FxRate | None:
    """Most recent USD/CAD rate, or None if it is older than the configured limit."""
    row = conn.execute(
        "SELECT rate, rate_date, source FROM fx_rates WHERE pair = 'USDCAD' "
        "ORDER BY rate_date DESC LIMIT 1").fetchone()
    if row is None:
        return None
    if local_date(now) - date.fromisoformat(row["rate_date"]) > timedelta(days=s.max_fx_age_days):
        return None
    return FxRate(D(row["rate"]), row["rate_date"], row["source"])


def watchlist_tickers(conn: sqlite3.Connection, s: Settings) -> set[str]:
    """Watchlist tickers in play. With CAD only on, US listings are left out."""
    tickers = {row["ticker"] for row in conn.execute("SELECT ticker FROM watchlist")}
    return {t for t in tickers if not s.cad_only or currency_for(t) == "CAD"}


def quote_is_stale(quote, s: Settings, now: datetime) -> bool:
    """End-of-day prices are judged in days, so a weekend does not stop them; intraday
    prices are judged in minutes, which also catches nights and market holidays."""
    age = age_minutes(quote["quote_ts"], now)
    limit = s.max_eod_age_days * 1440 if quote["end_of_day"] else s.max_quote_age_minutes
    return age > limit


def quote_status(quote, s: Settings, now: datetime) -> str:
    if quote is None or quote["price"] is None:
        return "no data"
    if quote["status"] != "ok":
        return "unavailable (last known)"
    if quote_is_stale(quote, s, now):
        return "stale"
    return "end of day" if quote["end_of_day"] else "ok"


def _holding(pos: Position, quote, fx: FxRate | None, s: Settings, now: datetime) -> HoldingView:
    status = quote_status(quote, s, now)
    price = D(quote["price"]) if quote is not None and quote["price"] is not None else None
    rate = D(1) if pos.currency == "CAD" else (fx.rate if fx else None)
    market_value = unrealized = None
    if price is not None and rate is not None:
        market_value = pos.quantity * price * rate
        unrealized = market_value - pos.book_cost
    elif price is not None:
        status = "no FX rate"
    return HoldingView(pos.ticker, pos.currency, pos.quantity, pos.avg_cost, pos.book_cost, price,
                       quote["exchange"] if quote is not None else None,
                       quote["quote_ts"] if quote is not None else None,
                       status, market_value, unrealized, rate)


def account_view(conn: sqlite3.Connection, account: str, s: Settings, now: datetime) -> AccountView:
    state = load_state(conn, account, s)
    quotes = load_quotes(conn)
    fx = latest_fx(conn, s, now)
    view = AccountView(account, s.start, state)
    for ticker in sorted(state.positions):
        pos = state.positions[ticker]
        holding = _holding(pos, quotes.get(ticker), fx, s, now)
        view.holdings.append(holding)
        if holding.market_value is None:
            view.market_value += pos.book_cost
            view.missing.append(ticker)
        else:
            view.market_value += holding.market_value
            view.unrealized += holding.unrealized
            if holding.status.startswith("unavailable"):
                view.missing.append(ticker)
    return view


def imported_views(conn: sqlite3.Connection, state: AccountState, s: Settings,
                   now: datetime) -> list[HoldingView]:
    """Imported holdings, valued in their own currency (market_value is not CAD here)."""
    quotes = load_quotes(conn)
    views = []
    for ticker in sorted(state.imported):
        pos = state.imported[ticker]
        quote = quotes.get(ticker)
        view = _holding(pos, quote, FxRate(D(1), "", ""), s, now)
        if view.price is not None:
            view.market_value = pos.quantity * view.price
            view.unrealized = view.market_value - pos.book_cost
        view.fx_rate = None
        views.append(view)
    return views


def budget(conn: sqlite3.Connection, account: str, s: Settings, now: datetime) -> Budget:
    trades = load_trades(conn, account)
    used = period_used(trades, period_start(now, s.reset_period))
    remaining = max(ZERO, s.limit - used)
    cash = replay(s.start, trades).cash
    return Budget(s.limit, used, remaining, min(cash, remaining), s.reset_period,
                  next_reset(now, s.reset_period))
