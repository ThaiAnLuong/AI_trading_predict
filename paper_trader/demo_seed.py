"""Fills the demo database with clearly fake history, by replaying past days through
the real engine, AI cycle and snapshots. Runs only in demo mode, on its own database."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from .ai.runner import run_cycle
from .context import AppContext
from .engine import TradeRejected, TradeRequest
from .performance import take_snapshots
from .service import refresh_bars, refresh_market, refresh_news
from .settings import save_settings
from .timeutil import local_date, to_iso
from .valuation import load_state

DEMO_WATCHLIST = (
    ("DEMA.TO", "Demo Maple Bank"), ("DEMB.TO", "Demo Prairie Energy"),
    ("DEMC.TO", "Demo Cloud CDR"), ("DEMD.TO", "Demo Chips CDR"), ("DEME.NE", "Demo Retail CDR"),
)
HISTORY_DAYS = 70


def _manual_trade(ctx: AppContext, rng: random.Random, tickers: list[str]) -> None:
    state = load_state(ctx.db.conn(), "MY", ctx.settings())
    if state.positions and rng.random() < 0.35:
        pos = rng.choice(list(state.positions.values()))
        request = TradeRequest("MY", "MANUAL", "SELL", pos.ticker, pos.quantity,
                               "DEMO: scripted sample sale")
    else:
        request = TradeRequest("MY", "MANUAL", "BUY", rng.choice(tickers), rng.randint(3, 12),
                               "DEMO: scripted sample purchase")
    try:
        ctx.engine.submit(request)
    except TradeRejected:
        pass


def seed_demo(ctx: AppContext) -> None:
    if not ctx.demo:
        raise RuntimeError("Demo data can only be written to the demo database.")
    if ctx.db.get_meta("demo_seeded"):
        return
    now = ctx.clock()
    with ctx.db.tx() as conn:
        for ticker, name in DEMO_WATCHLIST:
            conn.execute("INSERT OR IGNORE INTO watchlist(ticker, name, added_ts) VALUES (?,?,?)",
                         (ticker, name, to_iso(now)))
    s = ctx.settings()
    s.ai_enabled = True
    save_settings(ctx.db, s, ctx.clock)
    market = ctx.market()
    tickers = [t for t, _ in DEMO_WATCHLIST]
    for ticker in tickers:
        refresh_bars(ctx, market, ticker)

    rng = random.Random(7)
    start = local_date(now) - timedelta(days=HISTORY_DAYS)
    ctx.db.set_meta("experiment_started_at", to_iso(
        datetime(start.year, start.month, start.day, 9, 0).astimezone()))
    for offset in range(HISTORY_DAYS):
        day = start + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        ctx.clock.override = datetime(day.year, day.month, day.day, 15, 30).astimezone()
        refresh_market(ctx)
        refresh_news(ctx)
        run_cycle(ctx)
        if rng.random() < 0.3:
            _manual_trade(ctx, rng, tickers)
        take_snapshots(ctx.db, ctx.clock)
    ctx.clock.override = None
    ctx.engine.import_holding("DEMC.TO", 5, "90", "DEMO: sample imported holding")
    ctx.db.set_meta("demo_seeded", "1")
