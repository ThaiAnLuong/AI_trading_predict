"""The trade engine: the only code that writes to the trades table.

Manual and AI trades pass through the same checks inside one write transaction.
There is no brokerage code here or anywhere else; a "trade" is a row in a local
database.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_UP, Decimal

from .db import Database
from .ledger import AccountState, period_used
from .money import (D, PRICE_STEP, QTY_STEP, ZERO, cost_cents, fmt_money, fmt_qty, parse_decimal,
                    proceeds_cents)
from .settings import Settings, load_settings
from .symbols import cad_only_message, currency_for, normalize_ticker
from .timeutil import Clock, period_start, to_iso
from .valuation import account_view, latest_fx, load_state, load_trades, quote_is_stale

DUPLICATE_WINDOW_SECONDS = 10


class TradeRejected(Exception):
    """A rule stopped the trade. `str(exc)` is written for the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NeedsConfirmation(TradeRejected):
    """A manual trade may go ahead only if the user explicitly confirms it."""


@dataclass
class TradeRequest:
    account: str             # 'MY' or 'AI'
    source: str              # 'MANUAL' or 'AI'
    side: str                # 'BUY' or 'SELL'
    ticker: str
    quantity: object
    reason: str
    client_id: str = ""
    sleeve: str = "TRADING"  # 'IMPORTED' sells a holding added with import_holding
    decision_id: int | None = None
    confirm_stale: bool = False
    confirm_duplicate: bool = False


@dataclass
class TradePlan:
    """What a trade would do. Produced by preview() and by submit()."""
    request: TradeRequest
    ticker: str
    quantity: Decimal
    currency: str
    exchange: str | None
    reference_price: Decimal
    fill_price: Decimal
    slippage: Decimal
    slippage_rule: str
    fx_rate: Decimal | None
    fx_date: str | None
    fx_source: str | None
    gross_native: Decimal
    cash_delta: Decimal
    quote_ts: str
    quote_source: str
    stale: bool
    cash_after: Decimal
    end_of_day: bool = False


def slippage_for(ticker: str, price: Decimal, s: Settings) -> tuple[Decimal, str]:
    """Simulated per-share adjustment. A modelling assumption, not a broker fee."""
    category = s.ticker_categories.get(ticker, s.default_category)
    rule = s.slippage_categories[category]
    value = D(rule["value"])
    if rule["mode"] == "pct":
        return (price * value / 100).quantize(PRICE_STEP, rounding=ROUND_UP), f"{category}: {value}%"
    return value, f"{category}: {value}/share"


class TradeEngine:
    def __init__(self, db: Database, clock: Clock, demo: bool):
        self.db = db
        self.clock = clock
        self.demo = demo

    # -- public API ---------------------------------------------------------

    def preview(self, req: TradeRequest) -> TradePlan:
        return self._plan(self.db.conn(), req, load_settings(self.db), check_duplicates=False)

    def submit(self, req: TradeRequest) -> int:
        with self.db.tx() as conn:
            return self.submit_in(conn, req)

    def submit_in(self, conn: sqlite3.Connection, req: TradeRequest) -> int:
        """Validate and record inside the caller's open write transaction."""
        plan = self._plan(conn, req, load_settings(self.db), check_duplicates=True)
        return self._record(conn, plan)

    def import_holding(self, ticker: str, quantity, avg_price, note: str = "",
                       client_id: str = "") -> int:
        """Add an existing holding to My account's separate imported group.

        It takes no trading cash. Its value and any sale proceeds stay outside the
        trading account, so an import can never create buying power.
        """
        ticker = normalize_ticker(ticker)
        qty = parse_decimal(quantity, "Quantity")
        price = parse_decimal(avg_price, "Average purchase price")
        s = load_settings(self.db)
        self._check_cad_only(ticker, s)
        self._check_quantity(qty, s)
        if price <= 0:
            raise TradeRejected("BAD_PRICE", "Average purchase price must be greater than zero.")
        client_id = client_id or f"import:{uuid.uuid4()}"
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM trades WHERE client_id = ?", (client_id,)).fetchone():
                raise TradeRejected("DUPLICATE", "This import was already recorded.")
            cursor = conn.execute(
                "INSERT INTO trades (client_id, ts, account, source, side, sleeve, ticker, currency,"
                " quantity, fill_price, gross_native, cash_delta, reason)"
                " VALUES (?, ?, 'MY', 'IMPORT', 'IMPORT', 'IMPORTED', ?, ?, ?, ?, ?, '0', ?)",
                (client_id, to_iso(self.clock()), ticker, currency_for(ticker), str(qty),
                 str(price), str(qty * price), note.strip() or "Imported existing holding"))
            return cursor.lastrowid

    # -- checks -------------------------------------------------------------

    @staticmethod
    def _check_quantity(qty: Decimal, s: Settings) -> None:
        if qty <= 0:
            raise TradeRejected("BAD_QUANTITY", "Quantity must be greater than zero.")
        if qty > 1_000_000_000:
            raise TradeRejected("BAD_QUANTITY", "Quantity is too large.")
        if not s.allow_fractional and qty != qty.to_integral_value():
            raise TradeRejected(
                "BAD_QUANTITY", "Fractional shares are turned off in Settings. Use a whole number.")
        if qty != qty.quantize(QTY_STEP):
            raise TradeRejected("BAD_QUANTITY", "Quantity can have at most 4 decimal places.")

    def _check_request(self, req: TradeRequest, s: Settings) -> tuple[str, Decimal]:
        if (req.account, req.source) not in (("MY", "MANUAL"), ("AI", "AI")):
            raise TradeRejected(
                "WRONG_ACCOUNT", "AI trades can only use the AI account, and manual trades only "
                "My account.")
        if req.side not in ("BUY", "SELL"):
            raise TradeRejected("BAD_SIDE", "Action must be BUY or SELL.")
        if req.sleeve == "IMPORTED" and (req.side != "SELL" or req.account != "MY"):
            raise TradeRejected("BAD_SLEEVE", "Imported holdings can only be sold, from My account.")
        if not req.reason.strip():
            raise TradeRejected("NO_REASON", "Enter a reason for this trade.")
        try:
            ticker = normalize_ticker(req.ticker)
            qty = parse_decimal(req.quantity, "Quantity")
        except ValueError as exc:
            raise TradeRejected("BAD_INPUT", str(exc)) from None
        self._check_quantity(qty, s)
        if req.account == "AI" and not s.ai_enabled:
            raise TradeRejected("AI_OFF", "AI trading is switched off.")
        if req.side == "BUY":  # sells stay open, so a position can always be closed
            self._check_cad_only(ticker, s)
        return ticker, qty

    @staticmethod
    def _check_cad_only(ticker: str, s: Settings) -> None:
        if s.cad_only and currency_for(ticker) != "CAD":
            raise TradeRejected("CAD_ONLY", cad_only_message(ticker))

    def _check_duplicates(self, conn, req: TradeRequest, ticker: str, qty: Decimal) -> None:
        if req.client_id and conn.execute(
                "SELECT 1 FROM trades WHERE client_id = ?", (req.client_id,)).fetchone():
            raise TradeRejected("DUPLICATE", "This trade was already recorded.")
        if req.source != "MANUAL" or req.confirm_duplicate:
            return
        since = to_iso(self.clock() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
        rows = conn.execute(
            "SELECT quantity FROM trades WHERE account = ? AND ticker = ? AND side = ? AND ts >= ?",
            (req.account, ticker, req.side, since)).fetchall()
        if any(D(row["quantity"]) == qty for row in rows):
            raise NeedsConfirmation(
                "POSSIBLE_DUPLICATE",
                f"An identical {req.side} of {fmt_qty(qty)} {ticker} was recorded seconds ago. "
                "Record it again?")

    def _quote(self, conn, req: TradeRequest, ticker: str, s: Settings):
        quote = conn.execute("SELECT * FROM quotes WHERE ticker = ?", (ticker,)).fetchone()
        if quote is None or quote["status"] != "ok" or quote["price"] is None:
            detail = f" ({quote['error']})" if quote is not None and quote["error"] else ""
            raise TradeRejected(
                "NO_PRICE", f"No current price is available for {ticker}{detail}. The app will not "
                "guess a price, so this trade cannot be simulated.")
        if bool(quote["is_demo"]) != self.demo:
            raise TradeRejected("DEMO_MISMATCH", "Demo prices cannot be used with real data.")
        if D(quote["price"]) <= 0:
            raise TradeRejected("NO_PRICE", f"The stored price for {ticker} is not usable.")
        if quote["currency"] != currency_for(ticker):
            raise TradeRejected(
                "BAD_CURRENCY", f"{ticker} is expected in {currency_for(ticker)} but the quote is "
                f"in {quote['currency']}. The trade was blocked rather than mixing currencies.")
        stale = quote_is_stale(quote, s, self.clock())
        if stale and req.source == "AI":
            raise TradeRejected("STALE_QUOTE", f"The {ticker} quote is too old for AI trading.")
        if stale and not req.confirm_stale:
            raise NeedsConfirmation(
                "STALE_QUOTE", f"The latest {ticker} price is older than the limit in Settings "
                "(the market may be closed). Use it anyway? The trade will be flagged as using "
                "a stale quote.")
        return quote, stale

    def _check_ai_limits(self, conn, plan: TradePlan, state: AccountState, s: Settings) -> None:
        cost = -plan.cash_delta
        if cost > D(s.max_trade_size):
            raise TradeRejected(
                "AI_MAX_TRADE", f"Trade of {fmt_money(cost)} exceeds the maximum trade size of "
                f"{fmt_money(s.max_trade_size)}.")
        now = self.clock()
        since = to_iso(period_start(now, "daily"))
        buys = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE account = 'AI' AND side = 'BUY' AND ts >= ?",
            (since,)).fetchone()["n"]
        if buys >= s.max_daily_buys:
            raise TradeRejected("AI_MAX_BUYS", f"Maximum daily buys ({s.max_daily_buys}) reached.")
        if plan.ticker not in state.positions and len(state.positions) >= s.max_positions:
            raise TradeRejected("AI_MAX_POSITIONS", f"Maximum positions ({s.max_positions}) reached.")
        view = account_view(conn, "AI", s, now)
        ceiling = view.total_value * D(s.max_exposure_pct) / 100
        if view.market_value + cost > ceiling:
            raise TradeRejected(
                "AI_MAX_EXPOSURE", f"Trade would take exposure above {s.max_exposure_pct}% of the "
                f"account ({fmt_money(ceiling)}).")

    # -- planning -----------------------------------------------------------

    def _plan(self, conn, req: TradeRequest, s: Settings, check_duplicates: bool) -> TradePlan:
        ticker, qty = self._check_request(req, s)
        if check_duplicates:
            self._check_duplicates(conn, req, ticker, qty)
        quote, stale = self._quote(conn, req, ticker, s)

        reference = D(quote["price"])
        slip, rule = slippage_for(ticker, reference, s)
        if req.side == "BUY":
            fill = reference + slip
        else:
            fill = max(reference - slip, PRICE_STEP)
            slip = reference - fill
        gross = qty * fill
        currency = quote["currency"]
        state = load_state(conn, req.account, s)

        fx = None
        rate = D(1)
        if req.sleeve == "TRADING" and currency != "CAD":
            fx = latest_fx(conn, s, self.clock())
            if fx is None:
                raise TradeRejected(
                    "NO_FX", f"No recent USD/CAD rate is available, so {ticker} (USD) cannot be "
                    "converted against the CAD cash balance.")
            rate = fx.rate

        if req.sleeve == "IMPORTED":
            delta = ZERO
            held = state.imported.get(ticker)
        elif req.side == "BUY":
            delta = -cost_cents(gross * rate)
            held = None
        else:
            delta = proceeds_cents(gross * rate)
            held = state.positions.get(ticker)

        if req.side == "SELL":
            available = held.quantity if held else ZERO
            if qty > available:
                raise TradeRejected(
                    "OVERSELL", f"Cannot sell {fmt_qty(qty)} {ticker}: only {fmt_qty(available)} "
                    f"held{' in imported holdings' if req.sleeve == 'IMPORTED' else ''}.")
        else:
            cost = -delta
            if cost > state.cash:
                raise TradeRejected(
                    "NO_CASH", f"This buy costs {fmt_money(cost)} but only "
                    f"{fmt_money(state.cash)} cash is available.")
            used = period_used(load_trades(conn, req.account),
                               period_start(self.clock(), s.reset_period))
            remaining = max(ZERO, s.limit - used)
            if cost > remaining:
                raise TradeRejected(
                    "PERIOD_LIMIT", f"This buy costs {fmt_money(cost)} but only "
                    f"{fmt_money(remaining)} of the {s.reset_period} limit remains.")

        plan = TradePlan(req, ticker, qty, currency, quote["exchange"], reference, fill, slip, rule,
                         fx.rate if fx else None, fx.rate_date if fx else None,
                         fx.source if fx else None, gross, delta, quote["quote_ts"],
                         quote["source"], stale, state.cash + delta, bool(quote["end_of_day"]))
        if req.source == "AI" and req.side == "BUY":
            self._check_ai_limits(conn, plan, state, s)
        return plan

    def _record(self, conn, plan: TradePlan) -> int:
        req = plan.request
        cursor = conn.execute(
            "INSERT INTO trades (client_id, ts, account, source, side, sleeve, ticker, exchange,"
            " currency, quantity, reference_price, fill_price, slippage_per_share, slippage_rule,"
            " fx_rate, fx_date, fx_source, gross_native, cash_delta, quote_ts, quote_source,"
            " quote_stale, reason, decision_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (req.client_id or f"{req.source.lower()}:{uuid.uuid4()}", to_iso(self.clock()),
             req.account, req.source, req.side, req.sleeve, plan.ticker, plan.exchange,
             plan.currency, str(plan.quantity), str(plan.reference_price), str(plan.fill_price),
             str(plan.slippage), plan.slippage_rule,
             str(plan.fx_rate) if plan.fx_rate is not None else None, plan.fx_date, plan.fx_source,
             str(plan.gross_native), str(plan.cash_delta), plan.quote_ts, plan.quote_source,
             int(plan.stale), req.reason.strip(), req.decision_id))
        return cursor.lastrowid
