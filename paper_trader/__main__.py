"""Entry point:  python -m paper_trader [--demo] [--check-sources TICKER ...]"""
from __future__ import annotations

import argparse
import msvcrt
import sys
import traceback

from .config import PROJECT_ROOT, get_paths, load_api_keys
from .context import AppContext
from .market.base import DataUnavailable
from .market.registry import build_fx, build_market
from .news.registry import build_source
from .symbols import normalize_ticker


def check_sources(ctx: AppContext, tickers: list[str]) -> None:
    """Try each configured data source once and print what happened."""
    s = ctx.settings()
    market = build_market(s, ctx.demo, ctx.clock)
    print(f"Market provider: {market.name}")
    for raw in tickers:
        ticker = normalize_ticker(raw)
        try:
            q = market.get_quote(ticker)
            print(f"  {ticker}: OK {q.price} {q.currency} on {q.exchange}, as of {q.quote_ts}")
        except DataUnavailable as exc:
            print(f"  {ticker}: UNAVAILABLE - {exc}")
    try:
        fx = build_fx(ctx.demo, ctx.clock).get_usd_cad()
        print(f"USD/CAD: OK {fx.rate} for {fx.rate_date} ({fx.source})")
    except DataUnavailable as exc:
        print(f"USD/CAD: UNAVAILABLE - {exc}")
    for row in ctx.db.conn().execute("SELECT * FROM news_sources WHERE enabled = 1").fetchall():
        try:
            items = build_source(row, ctx.clock).fetch([normalize_ticker(t) for t in tickers])
            print(f"News '{row['name']}': OK {len(items)} item(s)")
        except DataUnavailable as exc:
            print(f"News '{row['name']}': UNAVAILABLE - {exc}")


def single_instance(paths):
    """Hold a lock so two copies cannot run the AI on the same database."""
    handle = open(paths.data_dir / f"{paths.db.stem}.lock", "a+b")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return None
    return handle


def main() -> int:
    parser = argparse.ArgumentParser(prog="paper_trader", description="Paper trading only.")
    parser.add_argument("--demo", action="store_true", help="fake data in a separate database")
    parser.add_argument("--data-dir", help="use a different data folder")
    parser.add_argument("--check-sources", nargs="*", metavar="TICKER",
                        help="test the configured data sources, then exit")
    args = parser.parse_args()

    load_api_keys()
    paths = get_paths(args.demo, args.data_dir)
    ctx = AppContext(paths)

    if args.check_sources is not None:
        check_sources(ctx, args.check_sources)
        return 0

    from tkinter import Tk, messagebox
    lock = single_instance(paths)
    if lock is None:
        root = Tk()
        root.withdraw()
        messagebox.showerror("Paper Trader", "Paper Trader is already running with this data.")
        return 1
    if args.demo:
        from .demo_seed import seed_demo
        seed_demo(ctx)
    from .ui.app import run
    run(ctx)
    lock.close()
    return 0


def report_crash() -> None:
    """pythonw has no console, so a startup failure must be written down and shown."""
    detail = traceback.format_exc()
    log = PROJECT_ROOT / "crash.log"
    log.write_text(detail, encoding="utf-8")
    try:
        from tkinter import Tk, messagebox
        root = Tk()
        root.withdraw()
        messagebox.showerror("Paper Trader", f"Could not start. Details are in:\n{log}")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        report_crash()
        sys.exit(1)
