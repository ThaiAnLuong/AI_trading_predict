"""One AI cycle: read data, decide, size, validate through the engine, log everything.

The runner only ever submits to the AI account. A decision and its trade are saved in
one transaction, so a crash cannot leave one without the other.
"""
from __future__ import annotations

import json
import uuid
from decimal import ROUND_DOWN, Decimal

from ..context import AppContext
from ..engine import TradeRejected, TradeRequest, slippage_for
from ..money import CENT, D, QTY_STEP, ZERO
from ..news.signals import qualifying_signals
from ..settings import Settings
from ..timeutil import local_date, period_start, to_iso
from ..valuation import (TRADABLE_STATUS, account_view, budget, latest_fx, load_state,
                         quote_status, watchlist_tickers)
from .llm_review import ReviewUnavailable
from .strategy import Proposal, TickerInput, to_float


def _sources(quote, bars: int, proposal: Proposal, strategy: str, review: str | None) -> str:
    return json.dumps({
        "strategy": strategy,
        "quote": {"source": quote["source"], "quote_ts": quote["quote_ts"]} if quote else None,
        "daily_bars": bars,
        "metrics": proposal.metrics,
        "news_unverified": [
            {"id": x.id, "title": x.title, "source": x.source, "published": x.published_ts,
             "url": x.url, "labels": list(x.labels)} for x in proposal.signals],
        "llm_review": review,
    }, sort_keys=True)


class Cycle:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.db = ctx.db
        self.s: Settings = ctx.settings()
        self.now = ctx.clock()
        self.id = uuid.uuid4().hex[:12]
        self.conn = ctx.db.conn()
        self.executed = 0

    def tickers(self) -> list[str]:
        held = set(load_state(self.conn, "AI", self.s).positions)
        return sorted(watchlist_tickers(self.conn, self.s) | held)

    def run(self) -> int:
        for ticker in self.tickers():
            try:
                self.decide(ticker)
            except Exception as exc:  # one bad ticker must not block a stop-loss on the next
                self.conn.execute(
                    "INSERT INTO update_log (ts, kind, status, detail) VALUES (?,'ai','error',?)",
                    (to_iso(self.now), f"{ticker}: {type(exc).__name__}: {exc}"[:500]))
        return self.executed

    # -- logging ------------------------------------------------------------

    def _is_repeat(self, ticker, action, status, explanation) -> bool:
        last = self.conn.execute(
            "SELECT action, status, explanation FROM ai_decisions WHERE ticker = ?"
            " ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        return (last is not None and tuple(last) == (action, status, explanation))

    def _insert(self, conn, ticker, proposal, status, sources, *, qty=None, quote=None,
                fill=None, rejection=None) -> int:
        cursor = conn.execute(
            "INSERT INTO ai_decisions (ts, cycle_id, ticker, action, quantity, reference_price,"
            " simulated_fill_price, currency, confidence, explanation, data_sources, status,"
            " rejection_reason, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (to_iso(self.now), self.id, ticker, proposal.action,
             str(qty) if qty is not None else None, quote["price"] if quote else None,
             str(fill) if fill is not None else None, quote["currency"] if quote else None,
             str(proposal.confidence) if proposal.confidence is not None else None,
             proposal.explanation, sources, status, rejection, self.ctx.strategy.name))
        decision_id = cursor.lastrowid
        for signal in proposal.signals:
            conn.execute("INSERT OR IGNORE INTO ai_decision_news VALUES (?, ?)",
                         (decision_id, signal.id))
        return decision_id

    def _note(self, ticker, proposal: Proposal, status: str, sources: str, quote=None) -> None:
        """Log a non-trade outcome, skipping exact repeats of the previous one."""
        if self._is_repeat(ticker, proposal.action, status, proposal.explanation):
            return
        with self.db.tx() as conn:
            self._insert(conn, ticker, proposal, status, sources, quote=quote)

    # -- decisions ----------------------------------------------------------

    def _input(self, ticker: str, quote) -> TickerInput:
        today = local_date(self.now).isoformat()
        bars = self.conn.execute(
            "SELECT close, volume FROM daily_bars WHERE ticker = ? AND date < ? ORDER BY date",
            (ticker, today)).fetchall()
        position = load_state(self.conn, "AI", self.s).positions.get(ticker)
        return TickerInput(
            ticker, D(quote["price"]), quote["currency"], to_float(b["close"] for b in bars),
            to_float(b["volume"] or 0 for b in bars), position,
            qualifying_signals(self.conn, ticker, self.now, self.s.news_lookback_hours),
            D(self.s.stop_loss_pct))

    def decide(self, ticker: str) -> None:
        quote = self.conn.execute("SELECT * FROM quotes WHERE ticker = ?", (ticker,)).fetchone()
        status = quote_status(quote, self.s, self.now)
        if status not in TRADABLE_STATUS:
            proposal = Proposal("HOLD", f"Paused for this ticker. Price status: {status}.")
            self._note(ticker, proposal, "PAUSED", _sources(quote, 0, proposal,
                                                            self.ctx.strategy.name, None))
            return

        data = self._input(ticker, quote)
        proposal = self.ctx.strategy.evaluate(data)
        review_note = None

        if proposal.action == "BUY":
            qty, why_not = self._size(ticker, quote)
            if why_not:
                proposal = Proposal("HOLD", why_not, None, proposal.metrics)
            else:
                proposal, review_note = self._review(data, proposal)
        elif proposal.action == "SELL":
            qty = data.position.quantity

        sources = _sources(quote, len(data.closes), proposal, self.ctx.strategy.name, review_note)
        if proposal.action == "HOLD":
            self._note(ticker, proposal, "HOLD", sources, quote)
        else:
            self._act(ticker, quote, proposal, qty, sources)

    def _size(self, ticker: str, quote) -> tuple[Decimal, str | None]:
        """Largest quantity inside every limit, or the reason no trade fits."""
        s = self.s
        since = to_iso(period_start(self.now, "daily"))
        buys = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE account = 'AI' AND side = 'BUY' AND ts >= ?",
            (since,)).fetchone()["n"]
        if buys >= s.max_daily_buys:
            return ZERO, "Buy signal skipped: maximum daily buys reached."
        view = account_view(self.conn, "AI", s, self.now)
        if len(view.state.positions) >= s.max_positions:
            return ZERO, "Buy signal skipped: maximum positions reached."
        room = view.total_value * D(s.max_exposure_pct) / 100 - view.market_value
        spend = min(D(s.max_trade_size), budget(self.conn, "AI", s, self.now).buying_power, room)
        rate = D(1)
        if quote["currency"] != "CAD":
            fx = latest_fx(self.conn, s, self.now)
            if fx is None:
                return ZERO, "Buy signal skipped: no recent USD/CAD rate."
            rate = fx.rate
        price = D(quote["price"])
        unit = (price + slippage_for(ticker, price, s)[0]) * rate
        step = QTY_STEP if s.allow_fractional else D(1)
        qty = ((spend - CENT) / unit).quantize(step, rounding=ROUND_DOWN) if spend > CENT else ZERO
        if qty <= 0:
            return ZERO, "Buy signal skipped: not enough room within the cash and exposure limits."
        return qty, None

    def _review(self, data: TickerInput, proposal: Proposal) -> tuple[Proposal, str | None]:
        """Apply the optional LLM check. Any failure means no buy."""
        s = self.s
        if not s.llm_review_enabled:
            return proposal, None
        day = local_date(self.now).isoformat()
        seen_key = f"llm_seen:{data.ticker}:{day}"
        count_key = f"llm_reviews:{day}"
        count = int(self.db.get_meta(count_key, "0"))
        if self.db.get_meta(seen_key):
            return Proposal("HOLD", "Buy skipped: this ticker was already reviewed today.", None,
                            proposal.metrics), None
        if count >= s.llm_max_reviews_per_day:
            return Proposal("HOLD", "Buy skipped: daily LLM review limit reached.", None,
                            proposal.metrics), None
        self.db.set_meta(seen_key, "1")
        self.db.set_meta(count_key, str(count + 1))
        try:
            review = self.ctx.reviewer(s).review(data, proposal)
        except ReviewUnavailable as exc:
            return Proposal("HOLD", f"Buy skipped: LLM review unavailable ({exc})", None,
                            proposal.metrics), f"unavailable: {exc}"
        note = f"{s.llm_model}: {'approved' if review.approve else 'vetoed'}"
        if not review.approve:
            return Proposal("HOLD", f"Vetoed by LLM review: {review.explanation}", None,
                            proposal.metrics, proposal.signals), note
        used = [x for x in proposal.signals if x.id in review.news_ids] or proposal.signals
        confidence = min(proposal.confidence, review.confidence)
        return Proposal("BUY", f"{proposal.explanation} LLM review: {review.explanation}",
                        confidence, proposal.metrics, used), note

    def _act(self, ticker, quote, proposal: Proposal, qty, sources) -> None:
        if (proposal.action == "BUY"
                and proposal.confidence < float(self.s.min_confidence)):
            proposal = Proposal("HOLD", "Buy skipped: confidence is below the minimum.", None,
                                proposal.metrics)
            self._note(ticker, proposal, "HOLD", sources, quote)
            return
        # One order per ticker, action and price update, even across restarts.
        client_id = f"ai:{ticker}:{proposal.action}:{quote['quote_ts']}"
        if self.conn.execute("SELECT 1 FROM trades WHERE client_id = ?", (client_id,)).fetchone():
            return
        request = TradeRequest("AI", "AI", proposal.action, ticker, qty,
                               proposal.explanation[:300], client_id=client_id)
        engine = self.ctx.engine
        if self.s.ai_suggest_only:
            self._suggest(ticker, quote, proposal, qty, sources, request)
            return
        with self.db.tx() as conn:
            decision_id = self._insert(conn, ticker, proposal, "REJECTED", sources, qty=qty,
                                       quote=quote)
            request.decision_id = decision_id
            try:
                trade_id = engine.submit_in(conn, request)
            except TradeRejected as exc:
                conn.execute("UPDATE ai_decisions SET rejection_reason = ? WHERE id = ?",
                             (str(exc), decision_id))
                return
            fill = conn.execute("SELECT fill_price FROM trades WHERE id = ?",
                                (trade_id,)).fetchone()["fill_price"]
            conn.execute(
                "UPDATE ai_decisions SET status = 'EXECUTED', trade_id = ?,"
                " simulated_fill_price = ? WHERE id = ?", (trade_id, fill, decision_id))
        self.executed += 1

    def _suggest(self, ticker, quote, proposal, qty, sources, request) -> None:
        """Suggest-only mode: run every check, record the outcome, trade nothing."""
        if self._is_repeat(ticker, proposal.action, "SUGGESTED", proposal.explanation):
            return
        try:
            plan = self.ctx.engine.preview(request)
        except TradeRejected as exc:
            with self.db.tx() as conn:
                self._insert(conn, ticker, proposal, "REJECTED", sources, qty=qty, quote=quote,
                             rejection=str(exc))
            return
        with self.db.tx() as conn:
            self._insert(conn, ticker, proposal, "SUGGESTED", sources, qty=qty, quote=quote,
                         fill=plan.fill_price)


def run_cycle(ctx: AppContext) -> int:
    """Run one cycle if AI trading is on. Returns the number of trades executed."""
    if not ctx.settings().ai_enabled:
        return 0
    return Cycle(ctx).run()
