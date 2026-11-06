"""Account state, always rebuilt by replaying the append-only trades table.

Nothing here touches the database. Cash and positions are never stored, so they
cannot drift away from the trade history.

Identity that holds after every trade:
    cash + book cost of holdings == starting balance + realized gain/loss
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping

from .money import D, ZERO, cents
from .timeutil import from_iso


@dataclass
class Position:
    ticker: str
    currency: str
    quantity: Decimal = ZERO
    avg_cost: Decimal = ZERO   # per share, in the ticker's own currency
    book_cost: Decimal = ZERO  # total, in CAD (imported holdings: own currency)


@dataclass
class AccountState:
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    realized: Decimal = ZERO
    imported: dict[str, Position] = field(default_factory=dict)
    # Imported holdings stay outside trading cash, tracked per currency.
    imported_realized: dict[str, Decimal] = field(default_factory=dict)
    imported_proceeds: dict[str, Decimal] = field(default_factory=dict)
    trade_count: int = 0


def _buy(pos: Position, qty: Decimal, gross: Decimal, book: Decimal) -> None:
    new_qty = pos.quantity + qty
    pos.avg_cost = (pos.quantity * pos.avg_cost + gross) / new_qty
    pos.quantity = new_qty
    pos.book_cost += book


def _sell(book: dict[str, Position], pos: Position, qty: Decimal) -> Decimal:
    """Reduce a position and return the book cost that was removed."""
    if qty >= pos.quantity:
        removed = pos.book_cost
        del book[pos.ticker]
    else:
        removed = cents(pos.book_cost * qty / pos.quantity)
        pos.quantity -= qty
        pos.book_cost -= removed
    return removed


def apply_trade(state: AccountState, t: Mapping) -> None:
    ticker, side, qty = t["ticker"], t["side"], D(t["quantity"])
    gross, delta = D(t["gross_native"]), D(t["cash_delta"])

    if t["sleeve"] == "IMPORTED":
        if side == "IMPORT":
            pos = state.imported.setdefault(ticker, Position(ticker, t["currency"]))
            _buy(pos, qty, gross, gross)
        else:
            currency = t["currency"]
            removed = _sell(state.imported, state.imported[ticker], qty)
            state.imported_realized[currency] = (
                state.imported_realized.get(currency, ZERO) + gross - removed)
            state.imported_proceeds[currency] = state.imported_proceeds.get(currency, ZERO) + gross
            state.trade_count += 1
        return

    state.cash += delta
    state.trade_count += 1
    if side == "BUY":
        pos = state.positions.setdefault(ticker, Position(ticker, t["currency"]))
        _buy(pos, qty, gross, -delta)
    else:
        state.realized += delta - _sell(state.positions, state.positions[ticker], qty)


def replay(starting_cash: Decimal, trades: Iterable[Mapping]) -> AccountState:
    state = AccountState(cash=starting_cash)
    for trade in trades:
        apply_trade(state, trade)
    return state


def period_used(trades: Iterable[Mapping], start: datetime) -> Decimal:
    """Net money newly deployed since `start`.

    Buys add their full cost. Sell proceeds reduce the figure but never below zero,
    so selling older holdings can never lift the remaining allowance above the limit.
    """
    used = ZERO
    for t in trades:
        if t["sleeve"] != "TRADING" or from_iso(t["ts"]) < start:
            continue
        delta = D(t["cash_delta"])
        used = used - delta if t["side"] == "BUY" else max(ZERO, used - delta)
    return used
