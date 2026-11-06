"""Background data updates. Each ticker is saved on its own, so an interrupted update
leaves earlier tickers intact and later ones marked with their previous state."""
from __future__ import annotations

import threading
import traceback
from datetime import timedelta
from typing import Callable

from .ai.lessons import write_lessons_file
from .ai.runner import run_cycle
from .context import AppContext
from .market.base import DataUnavailable, MarketDataProvider, Quote
from .news.registry import build_source
from .news.signals import load_categories, store_item
from .performance import snapshot_due, take_snapshots
from .symbols import currency_for
from .timeutil import from_iso, local_date, to_iso
from .valuation import load_state, watchlist_tickers


def log_update(ctx: AppContext, kind: str, status: str, detail: str = "") -> None:
    ctx.db.conn().execute("INSERT INTO update_log (ts, kind, status, detail) VALUES (?,?,?,?)",
                          (to_iso(ctx.clock()), kind, status, detail[:500]))


def tracked_tickers(ctx: AppContext) -> list[str]:
    conn = ctx.db.conn()
    s = ctx.settings()
    tickers = watchlist_tickers(conn, s)
    for account in ("MY", "AI"):
        state = load_state(conn, account, s)
        tickers |= set(state.positions) | set(state.imported)
    return sorted(tickers)


def needs_fx(ctx: AppContext) -> bool:
    """FX only matters if US tickers are allowed or a US position is still held."""
    return (not ctx.settings().cad_only
            or any(currency_for(t) != "CAD" for t in tracked_tickers(ctx)))


def store_quote(ctx: AppContext, quote: Quote, is_demo: bool) -> None:
    now = ctx.clock()
    # A date-only source can stamp a time later today. A quote is never from the future.
    quote_ts = min(quote.quote_ts, to_iso(now))
    expected = currency_for(quote.ticker)
    if quote.currency != expected:
        mark_unavailable(ctx, quote.ticker,
                         f"Quote currency {quote.currency} does not match expected {expected}.")
        return
    if quote.price <= 0:
        mark_unavailable(ctx, quote.ticker, "The source returned a non-positive price.")
        return
    ctx.db.conn().execute(
        "INSERT OR REPLACE INTO quotes (ticker, price, currency, exchange, quote_ts, fetched_ts,"
        " source, delay_note, status, error, volume, is_demo, end_of_day)"
        " VALUES (?,?,?,?,?,?,?,?,'ok',NULL,?,?,?)",
        (quote.ticker, str(quote.price), quote.currency, quote.exchange, quote_ts, to_iso(now),
         quote.source, quote.delay_note, str(quote.volume) if quote.volume is not None else None,
         int(is_demo), int(quote.end_of_day)))


def mark_unavailable(ctx: AppContext, ticker: str, error: str) -> None:
    """Keep the last known price for display, but block trading on it."""
    now = to_iso(ctx.clock())
    conn = ctx.db.conn()
    updated = conn.execute(
        "UPDATE quotes SET status = 'unavailable', error = ?, fetched_ts = ? WHERE ticker = ?",
        (error, now, ticker)).rowcount
    if not updated:
        conn.execute(
            "INSERT INTO quotes (ticker, currency, fetched_ts, status, error, is_demo)"
            " VALUES (?, ?, ?, 'unavailable', ?, ?)",
            (ticker, currency_for(ticker), now, error, int(ctx.demo)))


def refresh_fx(ctx: AppContext) -> None:
    try:
        fx = ctx.fx().get_usd_cad()
    except DataUnavailable as exc:
        log_update(ctx, "fx", "error", str(exc))
        return
    ctx.db.conn().execute(
        "INSERT OR REPLACE INTO fx_rates (pair, rate_date, rate, fetched_ts, source)"
        " VALUES ('USDCAD', ?, ?, ?, ?)",
        (fx.rate_date, str(fx.rate), to_iso(ctx.clock()), fx.source))


def _bars_current(ctx: AppContext, ticker: str) -> bool:
    return ctx.db.get_meta(f"bars:{ticker}") == local_date(ctx.clock()).isoformat()


def _save_bars(ctx: AppContext, market: MarketDataProvider, ticker: str, bars) -> None:
    today = local_date(ctx.clock()).isoformat()
    with ctx.db.tx() as conn:
        for bar in bars:
            conn.execute(
                "INSERT OR REPLACE INTO daily_bars (ticker, date, close, volume, source)"
                " VALUES (?,?,?,?,?)",
                (ticker, bar.date, str(bar.close),
                 str(bar.volume) if bar.volume is not None else None, market.name))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     (f"bars:{ticker}", today))


def refresh_bars(ctx: AppContext, market: MarketDataProvider, ticker: str) -> bool:
    """Fetch daily history at most once per ticker per day. Returns True if it called out."""
    if _bars_current(ctx, ticker):
        return False
    try:
        _save_bars(ctx, market, ticker, market.get_daily_bars(ticker))
    except DataUnavailable as exc:
        log_update(ctx, "bars", "error", f"{ticker}: {exc}")
    return True


def _update_end_of_day(ctx: AppContext, market: MarketDataProvider, ticker: str) -> int:
    """One call a day gives history and the latest close, which fits a ~25-call budget."""
    if _bars_current(ctx, ticker):
        return 0
    try:
        bars = market.get_daily_bars(ticker)
        quote = market.quote_from_bars(ticker, bars)
    except DataUnavailable as exc:  # not marked current, so the next refresh tries again
        mark_unavailable(ctx, ticker, str(exc))
        return 1
    _save_bars(ctx, market, ticker, bars)
    store_quote(ctx, quote, market.is_demo)
    return 1


def _update_intraday(ctx: AppContext, market: MarketDataProvider, ticker: str) -> int:
    try:
        store_quote(ctx, market.get_quote(ticker), market.is_demo)
    except DataUnavailable as exc:
        mark_unavailable(ctx, ticker, str(exc))
    return 1 + refresh_bars(ctx, market, ticker)


def update_ticker(ctx: AppContext, market: MarketDataProvider, ticker: str) -> int:
    """Refresh one ticker and return how many provider calls that took."""
    update = _update_end_of_day if market.end_of_day else _update_intraday
    return update(ctx, market, ticker)


def refresh_market(ctx: AppContext, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    market = ctx.market()
    for ticker in tracked_tickers(ctx):
        if stop.is_set():
            return
        calls = update_ticker(ctx, market, ticker)
        if calls and market.min_call_seconds and stop.wait(market.min_call_seconds * calls):
            return


def refresh_one(ctx: AppContext, ticker: str) -> None:
    """Fetch one ticker right now, for the trade ticket's preview."""
    if needs_fx(ctx) or currency_for(ticker) != "CAD":
        refresh_fx(ctx)
    update_ticker(ctx, ctx.market(), ticker)


def refresh_news(ctx: AppContext) -> None:
    conn = ctx.db.conn()
    watch = {r["ticker"]: r["name"] for r in conn.execute("SELECT ticker, name FROM watchlist")}
    categories = load_categories(conn)
    for row in conn.execute("SELECT * FROM news_sources WHERE enabled = 1").fetchall():
        now = to_iso(ctx.clock())
        try:
            items = build_source(row, ctx.clock).fetch(sorted(watch))
            with ctx.db.tx() as tx:
                for item in items:
                    store_item(tx, item, categories, watch, now)
            status = f"ok: {len(items)} item(s)"
        except DataUnavailable as exc:
            status = f"error: {exc}"
        conn.execute("UPDATE news_sources SET last_status = ?, last_fetch_ts = ? WHERE id = ?",
                     (status[:200], now, row["id"]))


def _due(ctx: AppContext, key: str, minutes: int) -> bool:
    last = ctx.db.get_meta(key)
    return last is None or ctx.clock() - from_iso(last) >= timedelta(minutes=minutes)


def update_once(ctx: AppContext, stop: threading.Event | None = None, force: bool = False) -> None:
    """One full update, doing whichever parts are due."""
    s = ctx.settings()
    if force or _due(ctx, "last_refresh", s.refresh_minutes):
        if needs_fx(ctx):
            refresh_fx(ctx)
        refresh_market(ctx, stop)
        if stop is not None and stop.is_set():
            return
        ctx.db.set_meta("last_refresh", to_iso(ctx.clock()))
        if force or _due(ctx, "last_news", s.news_minutes):
            refresh_news(ctx)
            ctx.db.set_meta("last_news", to_iso(ctx.clock()))
        trades = run_cycle(ctx)
        log_update(ctx, "refresh", "ok", f"AI trades this cycle: {trades}")
        try:
            write_lessons_file(ctx)
        except OSError as exc:  # the file may be locked by an editor; try again next time
            log_update(ctx, "lessons", "error", str(exc))
    if snapshot_due(ctx.db.conn(), ctx.settings(), ctx.clock()):
        take_snapshots(ctx.db, ctx.clock)


class Worker(threading.Thread):
    """Runs update_once in the background so the window stays responsive."""

    POLL_SECONDS = 30

    def __init__(self, ctx: AppContext, notify: Callable[[str], None]):
        super().__init__(daemon=True, name="paper-trader-updates")
        self.ctx = ctx
        self.notify = notify
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.force = True  # refresh right away on startup, so a restart recovers by itself
        self.idle = threading.Event()

    def request_refresh(self) -> None:
        self.force = True
        self.wake.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.wake.clear()
            self.idle.clear()
            force, self.force = self.force, False
            try:
                self.notify("updating")
                update_once(self.ctx, self.stop_event, force)
                self.notify("updated")
            except Exception as exc:  # the loop must survive anything
                log_update(self.ctx, "worker", "error",
                           f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                self.notify(f"error: {exc}")
            self.idle.set()
            self.wake.wait(self.POLL_SECONDS)
        self.ctx.db.close()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
