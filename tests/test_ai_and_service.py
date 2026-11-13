"""AI cycle, background updates, performance, export/backup/reset, restart safety."""
import csv
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from paper_trader import dataops, service
from paper_trader.ai.llm_review import Review, ReviewUnavailable, parse_review
from paper_trader.ai import strategy as strategy_module
from paper_trader.ai.runner import Cycle, run_cycle
from paper_trader.ai.strategy import MomentumNewsStrategy, TickerInput
from paper_trader.config import KEYS_FILE, get_paths, load_api_keys
from paper_trader.context import AppContext
from paper_trader.db import Database
from paper_trader.demo_seed import seed_demo
from paper_trader.engine import TradeRejected, TradeRequest
from paper_trader.ledger import Position
from paper_trader.market import alphavantage
from paper_trader.market.base import Bar, DataUnavailable, MarketDataProvider, Quote
from paper_trader.money import D
from paper_trader.news.base import NewsItem
from paper_trader.news.signals import Signal, load_categories, store_item, tally
from paper_trader.performance import (comparison, load_snapshots, matched_series, max_drawdown_pct,
                                      period_returns, take_snapshots)
from paper_trader.settings import load_settings, save_settings
from paper_trader.timeutil import local_date, to_iso
from paper_trader.valuation import account_view, load_state

from .helpers import FakeClock


def rising_closes(n=60, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


def data(price="135", closes=None, position=None, signals=()):
    closes = rising_closes() if closes is None else closes
    return TickerInput("RY.TO", D(price), "CAD", closes, [1000.0] * len(closes), position,
                       list(signals), D("8"))


def signal(direction, id=1, weight=1.0):
    return Signal(id, "t", "src", "2026-11-04T00:00:00.000000Z", "https://x.test", "", ("label",),
                  direction, weight)


def votes(good, bad, start=1):
    return ([signal(1, start + i) for i in range(good)]
            + [signal(-1, start + good + i) for i in range(bad)])


class NewsVote(unittest.TestCase):
    """Version 1.4.0: the bigger side of the news wins; equal is neutral."""
    s = MomentumNewsStrategy()

    def test_labels(self):
        self.assertEqual(tally(votes(6, 4)).label(), "Positive (6/4, +2)")
        self.assertEqual(tally(votes(5, 5)).label(), "Neutral (5/5, 0)")
        self.assertEqual(tally(votes(7, 7)).status, "Neutral")
        self.assertEqual(tally(votes(3, 11)).label(), "Negative (3/11, -8)")
        self.assertEqual(tally([]).label(), "No news")
        self.assertEqual(tally([signal(0)]).label(), "No news")  # neutral labels cast no vote
        half = tally([signal(1), signal(-1, 2, 0.5)])
        self.assertEqual(half.label(), "Positive (1/0.5, +0.5)")

    def test_one_bad_headline_no_longer_blocks_a_buy(self):
        result = self.s.evaluate(data(signals=votes(6, 4)))
        self.assertEqual(result.action, "BUY")
        self.assertIn("news Positive (6/4, +2)", result.explanation)
        self.assertEqual(result.plan.news, "Positive (6/4, +2)")
        self.assertEqual(len(result.signals), 10)

    def test_tie_buys_without_a_bonus(self):
        plain = self.s.evaluate(data()).confidence
        result = self.s.evaluate(data(signals=votes(5, 5)))
        self.assertEqual(result.action, "BUY")
        self.assertEqual(result.confidence, plain)

    def test_negative_majority_vetoes(self):
        result = self.s.evaluate(data(signals=votes(3, 11)))
        self.assertEqual(result.action, "HOLD")
        self.assertIn("Negative (3/11, -8)", result.explanation)

    def test_bigger_lead_more_confidence_capped(self):
        c = [self.s.evaluate(data(signals=votes(g, 0))).confidence for g in (1, 2, 3, 6)]
        self.assertLess(c[0], c[1])
        self.assertLess(c[1], c[2])
        self.assertEqual(c[2], c[3])  # capped at +0.15

    def test_sector_news_counts_half(self):
        # one good headline about the stock against one bad one from its sector: Positive
        result = self.s.evaluate(data(signals=[signal(1), signal(-1, 2, 0.5)]))
        self.assertEqual(result.action, "BUY")

    def test_negative_news_blocks_buy_more_and_shows_in_plan(self):
        closes = rising_closes(60, 100, 0.5)
        price = D(str(round(sum(closes[-20:]) / 20 * 1.005, 2)))  # just above the 20-day average

        def held_with(news):
            return self.s.evaluate(TickerInput("RY.TO", price, "CAD", closes, [1000.0] * 60,
                                               held(str(price)), news, D("8"), price, 1))
        result = held_with(votes(1, 2))
        self.assertEqual(result.action, "HOLD")
        self.assertEqual(result.explanation, "Buy more vetoed: news Negative (1/2, -1).")
        self.assertEqual(result.plan.news, "Negative (1/2, -1)")
        result = held_with(votes(2, 1))
        self.assertEqual(result.action, "BUY")
        self.assertIn("News Positive (2/1, +1)", result.explanation)


class Strategy(unittest.TestCase):
    s = MomentumNewsStrategy()

    def test_uptrend_buys(self):
        self.assertEqual(self.s.evaluate(data()).action, "BUY")

    def test_news_alone_cannot_trigger_a_trade(self):
        falling = list(reversed(rising_closes()))
        result = self.s.evaluate(data("95", falling, signals=[signal(1), signal(1, 2), signal(1, 3)]))
        self.assertEqual(result.action, "HOLD")

    def test_negative_news_vetoes_but_positive_raises_confidence(self):
        self.assertEqual(self.s.evaluate(data(signals=[signal(-1)])).action, "HOLD")
        self.assertGreater(self.s.evaluate(data(signals=[signal(1)])).confidence,
                           self.s.evaluate(data()).confidence)

    def test_stop_loss_needs_no_history(self):
        pos = Position("RY.TO", "CAD", D("10"), D("100"), D("1000"))
        result = self.s.evaluate(data("91.99", [], pos))
        self.assertEqual(result.action, "SELL")
        self.assertIn("Stop-loss", result.explanation)

    def test_short_history_holds(self):
        self.assertEqual(self.s.evaluate(data(closes=rising_closes(20))).action, "HOLD")


def held(cost, qty="10"):
    return Position("RY.TO", "CAD", D(qty), D(cost), D(cost) * D(qty))


def slow_slide_closes():
    """40 days up, then 20 days of small drops: never more than 2% in 5 days."""
    return rising_closes(40, 100, 0.5) + [119.5 - 0.3 * (i + 1) for i in range(20)]


class PlanRules(unittest.TestCase):
    """Version 1.2.0: exits for slow declines, a rising stop, and the plan for each holding."""
    s = MomentumNewsStrategy()

    def held_data(self, price, closes, cost, peak=None, buys=1):
        return TickerInput("RY.TO", D(price), "CAD", closes, [1000.0] * len(closes), held(cost),
                           [], D("8"), D(peak) if peak else None, buys)

    def test_slow_slide_is_sold(self):
        closes = slow_slide_closes()
        result = self.s.evaluate(self.held_data("113.3", closes, "112", peak="119.5"))
        self.assertGreater(result.metrics["momentum_5d_pct"], -2)  # the old rule would hold
        self.assertEqual(result.action, "SELL")
        self.assertIn("slow slide", result.explanation)

    def test_stop_rises_with_the_price(self):
        # Bought at 100, went to 120, now 110: the old fixed stop (92) would still hold.
        result = self.s.evaluate(self.held_data("110", [], "100", peak="120"))
        self.assertEqual(result.action, "SELL")
        self.assertIn("Trailing stop", result.explanation)
        self.assertIn("110.40", result.explanation)

    def test_no_peak_keeps_the_fixed_stop(self):
        result = self.s.evaluate(self.held_data("93", [], "100", peak="100"))
        self.assertEqual(result.action, "HOLD")
        self.assertEqual(result.plan.stop, D("92.00"))
        self.assertFalse(result.plan.trailing)

    def test_target_reached_sells(self):
        result = self.s.evaluate(self.held_data("135", rising_closes(), "110", peak="135"))
        self.assertEqual(result.action, "SELL")
        self.assertIn("Target reached", result.explanation)
        self.assertEqual(result.plan.target, D("127.60"))  # 110 + 2 x 8%

    def test_buys_more_on_a_dip_in_an_uptrend_once(self):
        result = self.s.evaluate(self.held_data("125.5", rising_closes(), "110", peak="125.5"))
        self.assertEqual(result.action, "BUY")
        self.assertIn("Buy more", result.explanation)
        self.assertEqual(result.plan.add_at, D("126.00"))
        again = self.s.evaluate(self.held_data("125.5", rising_closes(), "110", peak="125.5",
                                               buys=2))
        self.assertEqual(again.action, "HOLD")
        self.assertIsNone(again.plan.add_at)

    def test_never_buys_more_below_cost(self):
        result = self.s.evaluate(self.held_data("125.5", rising_closes(), "126", peak="126"))
        self.assertNotEqual(result.action, "BUY")

    def test_weak_holding_is_marked_sell_soon(self):
        flat = [100.0] * 60
        result = self.s.evaluate(self.held_data("99.5", flat, "100", peak="100"))
        self.assertEqual(result.action, "HOLD")
        self.assertEqual(result.plan.label(), "Sell soon (1-3 days)")

    def test_new_buy_carries_a_hold_plan_with_days(self):
        result = self.s.evaluate(data())
        self.assertEqual(result.action, "BUY")
        plan = result.plan
        self.assertEqual((plan.target, plan.stop), (D("156.60"), D("124.20")))
        # 20-day pace: (135 - 120) / 20 = 0.75 a day; 21.6 to go = about 29 days.
        self.assertEqual(plan.label(), "Hold (20-43 days)")
        self.assertIn("target 156.60", plan.short())

    def test_no_new_buy_right_after_selling(self):
        recent = TickerInput("RY.TO", D("135"), "CAD", rising_closes(), [1000.0] * 60, None, [],
                             D("8"), days_since_sell=1)
        result = self.s.evaluate(recent)
        self.assertEqual(result.action, "HOLD")
        self.assertIn("sold less than", result.explanation)


class FakeMarket(MarketDataProvider):
    name = "fake"
    is_demo = True

    def __init__(self, clock):
        self.clock = clock
        self.prices = {}
        self.fail = set()
        self.calls = 0

    def get_quote(self, ticker):
        self.calls += 1
        if ticker in self.fail:
            raise DataUnavailable("simulated outage")
        cur = "CAD" if ticker.endswith(".TO") else "USD"
        return Quote(ticker, D(self.prices[ticker]), cur, "X", to_iso(self.clock()), "fake", "note")

    def get_daily_bars(self, ticker):
        today = local_date(self.clock())
        return [Bar((today - timedelta(days=60 - i)).isoformat(), D(str(c)), D(1000))
                for i, c in enumerate(rising_closes())]


class AppCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="paper_trader_app_"))
        self.clock = FakeClock()
        self.ctx = self.make_ctx()
        self.market = FakeMarket(self.clock)
        self.market.prices = {"RY.TO": "135", "TD.TO": "135"}
        self.ctx.market = lambda: self.market
        self.configure(ai_enabled=True)
        self.ctx.db.conn().execute(
            "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('RY.TO', 'Royal Bank', 't')")
        self.ctx.db.conn().execute("DELETE FROM news_sources")

    def make_ctx(self):
        return AppContext(get_paths(True, self.dir), self.clock)

    def tearDown(self):
        self.ctx.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def configure(self, **changes):
        s = load_settings(self.ctx.db)
        for key, value in changes.items():
            setattr(s, key, value)
        save_settings(self.ctx.db, s, self.clock)

    def update(self):
        service.update_once(self.ctx, force=True)

    def decisions(self):
        return self.ctx.db.conn().execute("SELECT * FROM ai_decisions ORDER BY id").fetchall()

    def trades(self, account="AI"):
        return self.ctx.db.conn().execute("SELECT * FROM trades WHERE account = ?",
                                          (account,)).fetchall()


class AiCycle(AppCase):
    def test_full_cycle_records_decision_and_trade(self):
        self.update()
        trades = self.trades()
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["side"], t["source"], t["account"]), ("BUY", "AI", "AI"))
        self.assertEqual(t["quantity"], "7")  # floor(1000 / 135.03)
        d = self.decisions()[0]
        self.assertEqual(d["status"], "EXECUTED")
        self.assertEqual(d["trade_id"], t["id"])
        self.assertEqual(d["reference_price"], "135")
        self.assertEqual(d["simulated_fill_price"], "135.03")
        self.assertIsNotNone(d["confidence"])
        self.assertTrue(d["explanation"])
        self.assertEqual(json.loads(d["data_sources"])["daily_bars"], 60)

    def test_ai_never_touches_my_account(self):
        self.update()
        self.assertEqual(self.trades("MY"), [])
        s = load_settings(self.ctx.db)
        self.assertEqual(load_state(self.ctx.db.conn(), "MY", s).cash, D("10000"))

    def test_off_switch(self):
        self.configure(ai_enabled=False)
        self.update()
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.trades(), [])

    def test_suggest_only_records_but_does_not_trade(self):
        self.configure(ai_suggest_only=True)
        self.update()
        self.assertEqual(self.trades(), [])
        self.assertEqual(self.decisions()[0]["status"], "SUGGESTED")
        self.assertEqual(self.decisions()[0]["simulated_fill_price"], "135.03")

    def test_unavailable_price_pauses_only_that_ticker(self):
        self.ctx.db.conn().execute(
            "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('TD.TO', '', 't')")
        self.market.fail = {"RY.TO"}
        self.update()
        by_ticker = {d["ticker"]: d for d in self.decisions()}
        self.assertEqual(by_ticker["RY.TO"]["status"], "PAUSED")
        self.assertEqual(by_ticker["TD.TO"]["status"], "EXECUTED")
        self.assertEqual([t["ticker"] for t in self.trades()], ["TD.TO"])

    def test_stale_price_pauses_ai(self):
        self.update()
        self.market.fail = {"RY.TO"}  # market gone quiet: no new quote arrives
        self.clock.advance(hours=3)
        with mock.patch.object(service, "refresh_market"):
            self.update()
        self.assertEqual(self.decisions()[-1]["status"], "PAUSED")
        self.assertIn("stale", self.decisions()[-1]["explanation"])

    @mock.patch.object(strategy_module, "REENTRY_DAYS", 0)  # test the order guard, not the wait
    def test_restart_does_not_repeat_an_order(self):
        service.refresh_fx(self.ctx)
        service.refresh_market(self.ctx)
        self.assertEqual(run_cycle(self.ctx), 1)
        self.ctx.db.conn().execute("DELETE FROM ai_decisions")  # worst case: log lost
        s = load_settings(self.ctx.db)
        # Same quote, position closed: the cycle would want to buy again.
        self.ctx.engine.submit(TradeRequest("AI", "AI", "SELL", "RY.TO", 7, "test sale"))
        self.assertEqual(run_cycle(self.ctx), 0)
        self.assertEqual(len(self.trades()), 2)
        self.assertEqual(load_state(self.ctx.db.conn(), "AI", s).positions, {})

    def test_crash_mid_trade_leaves_nothing_half_written(self):
        service.refresh_fx(self.ctx)
        service.refresh_market(self.ctx)
        with mock.patch.object(self.ctx.engine, "_record", side_effect=RuntimeError("power cut")):
            self.assertEqual(run_cycle(self.ctx), 0)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.trades(), [])
        logged = self.ctx.db.conn().execute("SELECT detail FROM update_log WHERE kind='ai'").fetchone()
        self.assertIn("power cut", logged["detail"])

    def test_one_failing_ticker_does_not_stop_the_others(self):
        self.ctx.db.conn().execute(
            "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('TD.TO', '', 't')")
        service.refresh_fx(self.ctx)
        service.refresh_market(self.ctx)
        evaluate = self.ctx.strategy.evaluate

        def flaky(data):
            if data.ticker == "RY.TO":
                raise ValueError("bad data")
            return evaluate(data)

        with mock.patch.object(self.ctx.strategy, "evaluate", side_effect=flaky):
            self.assertEqual(run_cycle(self.ctx), 1)
        self.assertEqual([t["ticker"] for t in self.trades()], ["TD.TO"])

    def test_hold_decisions_are_not_repeated(self):
        self.configure(max_daily_buys=0)
        for _ in range(3):
            self.clock.advance(minutes=20)
            self.update()
        self.assertEqual(len(self.decisions()), 1)
        self.assertEqual(self.decisions()[0]["status"], "HOLD")

    def test_ai_respects_cash_exposure_limit_when_sizing(self):
        self.configure(max_exposure_pct="5")
        self.update()
        self.assertEqual(self.trades()[0]["quantity"], "3")  # 5% of 10,000 = 500

    def test_news_stored_and_linked_to_decision(self):
        conn = self.ctx.db.conn()
        store_item(conn, NewsItem("Royal Bank beats estimates", "Wire",
                                  to_iso(self.clock() - timedelta(hours=1)), "https://x.test/1",
                                  "summary"), load_categories(conn), {"RY.TO": "Royal Bank"},
                   to_iso(self.clock()))
        self.update()
        decision = self.decisions()[0]
        self.assertEqual(decision["status"], "EXECUTED")
        sources = json.loads(decision["data_sources"])["news_unverified"]
        self.assertEqual(sources[0]["labels"], ["earnings_beat"])
        self.assertEqual(sources[0]["source"], "Wire")
        links = conn.execute("SELECT * FROM ai_decision_news").fetchall()
        self.assertEqual(len(links), 1)

    def test_old_or_unlabelled_news_does_not_qualify(self):
        conn = self.ctx.db.conn()
        cats, watch = load_categories(conn), {"RY.TO": "Royal Bank"}
        store_item(conn, NewsItem("Royal Bank beats estimates", "W",
                                  to_iso(self.clock() - timedelta(days=9)), "https://x.test/old"),
                   cats, watch, to_iso(self.clock()))
        store_item(conn, NewsItem("Royal Bank opens a branch", "W", to_iso(self.clock()),
                                  "https://x.test/plain"), cats, watch, to_iso(self.clock()))
        self.update()
        self.assertEqual(json.loads(self.decisions()[0]["data_sources"])["news_unverified"], [])


class LlmReview(AppCase):
    def reviewer(self, result=None, error=None):
        fake = mock.Mock()
        fake.review.side_effect = error
        fake.review.return_value = result
        self.ctx.reviewer = lambda s: fake
        return fake

    def test_approval_trades_with_lower_confidence(self):
        self.configure(llm_review_enabled=True)
        self.reviewer(Review(True, 0.56, "Looks fine.", ()))
        self.update()
        d = self.decisions()[0]
        self.assertEqual(d["status"], "EXECUTED")
        self.assertEqual(d["confidence"], "0.55")
        self.assertIn("Looks fine.", d["explanation"])

    def test_veto_blocks_buy(self):
        self.configure(llm_review_enabled=True)
        self.reviewer(Review(False, 0.2, "Thin evidence.", ()))
        self.update()
        self.assertEqual(self.trades(), [])
        self.assertIn("Thin evidence", self.decisions()[0]["explanation"])

    def test_failure_means_no_buy(self):
        self.configure(llm_review_enabled=True)
        self.reviewer(error=ReviewUnavailable("down"))
        self.update()
        self.assertEqual(self.trades(), [])
        self.assertIn("unavailable", self.decisions()[0]["explanation"])

    def test_reviews_are_capped(self):
        self.configure(llm_review_enabled=True, llm_max_reviews_per_day=0)
        fake = self.reviewer(Review(True, 0.9, "ok", ()))
        self.update()
        fake.review.assert_not_called()
        self.assertEqual(self.trades(), [])

    def test_one_review_per_ticker_per_day(self):
        self.configure(llm_review_enabled=True)
        fake = self.reviewer(Review(False, 0.1, "no", ()))
        self.update()
        self.clock.advance(minutes=30)
        self.update()
        self.assertEqual(fake.review.call_count, 1)

    def test_parse_review_drops_unknown_news_ids(self):
        text = json.dumps({"approve": True, "confidence": 5, "explanation": "x",
                           "news_ids_used": [1, 99]})
        review = parse_review(text, {1})
        self.assertEqual((review.confidence, review.news_ids), (1.0, (1,)))

    def test_parse_review_rejects_garbage(self):
        for text in ("not json", "{}", json.dumps({"approve": True, "confidence": 1,
                                                   "explanation": " ", "news_ids_used": []})):
            with self.assertRaises(ReviewUnavailable):
                parse_review(text, set())


class Updates(AppCase):
    def test_outage_keeps_last_price_for_display_but_blocks_trading(self):
        self.update()
        self.market.fail = {"RY.TO"}
        self.clock.advance(minutes=20)
        self.update()
        q = self.ctx.db.conn().execute("SELECT * FROM quotes WHERE ticker='RY.TO'").fetchone()
        self.assertEqual((q["status"], q["price"], q["error"]), ("unavailable", "135", "simulated outage"))
        view = account_view(self.ctx.db.conn(), "AI", load_settings(self.ctx.db), self.clock())
        self.assertEqual(view.data_quality, "partial")
        with self.assertRaises(TradeRejected):
            self.ctx.engine.submit(TradeRequest("AI", "AI", "SELL", "RY.TO", 1, "x"))

    def test_interrupted_refresh_keeps_finished_tickers(self):
        self.ctx.db.conn().execute(
            "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('TD.TO', '', 't')")
        stop = threading.Event()
        original = self.market.get_quote

        def quote_then_stop(ticker):
            result = original(ticker)
            stop.set()
            return result

        self.market.get_quote = quote_then_stop
        self.market.min_call_seconds = 0.01
        service.refresh_market(self.ctx, stop)
        rows = self.ctx.db.conn().execute("SELECT ticker FROM quotes").fetchall()
        self.assertEqual([r["ticker"] for r in rows], ["RY.TO"])

    def test_wrong_currency_from_provider_is_not_stored_as_ok(self):
        service.store_quote(self.ctx, Quote("RY.TO", D("100"), "USD", "X", to_iso(self.clock()),
                                            "fake", "n"), True)
        q = self.ctx.db.conn().execute("SELECT * FROM quotes").fetchone()
        self.assertEqual(q["status"], "unavailable")

    def test_future_quote_time_is_clamped(self):
        future = to_iso(self.clock() + timedelta(hours=5))
        service.store_quote(self.ctx, Quote("RY.TO", D("100"), "CAD", "X", future, "fake", "n"), True)
        q = self.ctx.db.conn().execute("SELECT * FROM quotes").fetchone()
        self.assertEqual(q["quote_ts"], to_iso(self.clock()))

    def test_bars_fetched_once_a_day(self):
        with mock.patch.object(self.market, "get_daily_bars", wraps=self.market.get_daily_bars) as m:
            self.update()
            self.clock.advance(minutes=20)
            self.update()
            self.assertEqual(m.call_count, 1)

    def test_worker_survives_an_exception_and_stops_cleanly(self):
        events = []
        worker = service.Worker(self.ctx, events.append)
        with mock.patch.object(service, "update_once", side_effect=ValueError("boom")):
            worker.start()
            self.assertTrue(worker.idle.wait(5))
        worker.stop()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(e.startswith("error") for e in events))

    def test_data_survives_restart(self):
        self.update()
        before = len(self.trades())
        self.ctx.db.close()
        self.ctx = self.make_ctx()
        self.assertEqual(len(self.trades()), before)
        s = load_settings(self.ctx.db)
        self.assertTrue(s.ai_enabled)
        self.assertEqual(len(load_state(self.ctx.db.conn(), "AI", s).positions), 1)


class EndOfDayMarket(FakeMarket):
    end_of_day = True

    def __init__(self, clock):
        super().__init__(clock)
        self.history_calls = 0

    def get_quote(self, ticker):
        raise AssertionError("end-of-day sources must not spend a call on quotes")

    def get_daily_bars(self, ticker):
        self.history_calls += 1
        if ticker in self.fail:
            raise DataUnavailable("daily limit reached")
        return super().get_daily_bars(ticker)

    def quote_from_bars(self, ticker, bars):
        return alphavantage.quote_from_bars(ticker, bars, "fake")


class EndOfDaySource(AppCase):
    def setUp(self):
        super().setUp()
        self.market = EndOfDayMarket(self.clock)
        self.ctx.market = lambda: self.market

    def test_one_call_per_ticker_per_day_however_often_it_refreshes(self):
        for _ in range(5):
            self.update()
            self.clock.advance(minutes=30)
        self.assertEqual(self.market.history_calls, 1)
        self.clock.advance(days=1)
        self.update()
        self.assertEqual(self.market.history_calls, 2)

    def test_ai_trades_on_the_close_once_and_is_not_paused(self):
        self.update()
        self.clock.advance(minutes=30)
        self.update()
        self.assertEqual(self.decisions()[0]["status"], "EXECUTED")
        self.assertEqual(len(self.trades()), 1)
        q = self.ctx.db.conn().execute("SELECT * FROM quotes").fetchone()
        self.assertEqual(q["end_of_day"], 1)
        self.assertEqual(q["status"], "ok")

    def test_failed_call_is_retried_at_the_next_refresh(self):
        self.market.fail = {"RY.TO"}
        self.update()
        self.clock.advance(minutes=30)
        self.market.fail = set()
        self.update()
        self.assertEqual(self.market.history_calls, 2)
        q = self.ctx.db.conn().execute("SELECT status FROM quotes").fetchone()
        self.assertEqual(q["status"], "ok")


class CadOnlyService(AppCase):
    def test_us_ticker_on_old_watchlist_is_skipped_and_no_fx_is_fetched(self):
        self.ctx.db.conn().execute(
            "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('AAPL', '', 't')")
        fx = mock.Mock()
        self.ctx.fx = lambda: fx
        self.update()
        self.assertEqual(service.tracked_tickers(self.ctx), ["RY.TO"])
        self.assertEqual({d["ticker"] for d in self.decisions()}, {"RY.TO"})
        fx.get_usd_cad.assert_not_called()

    def test_turning_it_off_brings_us_tickers_and_fx_back(self):
        self.ctx.db.conn().execute(
            "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('AAPL', '', 't')")
        self.configure(cad_only=False)
        self.assertEqual(service.tracked_tickers(self.ctx), ["AAPL", "RY.TO"])
        self.assertTrue(service.needs_fx(self.ctx))

    def test_old_database_gains_the_new_column(self):
        path = self.dir / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE quotes (ticker TEXT PRIMARY KEY, fetched_ts TEXT, status TEXT)")
        conn.commit()
        conn.close()
        db = Database(path)
        names = {r["name"] for r in db.conn().execute("PRAGMA table_info(quotes)")}
        self.assertIn("end_of_day", names)
        db.close()


class Performance(AppCase):
    def test_snapshots_exist_without_trades(self):
        self.configure(ai_enabled=False)
        for _ in range(3):
            self.update()
            self.clock.advance(days=1)
        self.assertEqual(len(load_snapshots(self.ctx.db.conn(), "MY")), 3)
        self.assertEqual(len(matched_series(self.ctx.db.conn())), 3)

    def test_snapshots_throttled(self):
        self.update()
        self.clock.advance(minutes=20)
        self.update()
        self.assertEqual(len(load_snapshots(self.ctx.db.conn(), "MY")), 1)

    def test_drawdown(self):
        values = [D(x) for x in (100, 120, 90, 110, 80, 130)]
        self.assertEqual(max_drawdown_pct(values), D(40) / D(120) * 100)
        self.assertEqual(max_drawdown_pct([D(1), D(2), D(3)]), 0)

    def test_period_returns_need_real_history(self):
        take_snapshots(self.ctx.db, self.clock)
        self.clock.advance(days=8)
        snaps = load_snapshots(self.ctx.db.conn(), "MY")
        results = {r.label: r for r in period_returns(snaps, D("11000"), self.clock())}
        self.assertEqual(results["Weekly"].pct, D("10"))
        self.assertIsNone(results["Monthly"].pct)
        self.assertIn("history", results["Monthly"].note)

    def test_comparison_uses_same_dates_and_start(self):
        self.update()
        self.clock.advance(days=1)
        self.market.prices["RY.TO"] = "150"
        self.update()
        rows = {r.account: r for r in comparison(self.ctx.db.conn(), load_settings(self.ctx.db))}
        self.assertEqual(rows["MY"].total_value, D("10000"))
        self.assertGreater(rows["AI"].total_value, D("10000"))
        self.assertEqual(rows["AI"].trades, 1)
        self.assertEqual(rows["MY"].trades, 0)


class DataOps(AppCase):
    def test_export_all_files_and_formula_safety(self):
        conn = self.ctx.db.conn()
        store_item(conn, NewsItem("=HYPERLINK(1)", "W", to_iso(self.clock()), "https://x.test/f"),
                   load_categories(conn), {}, to_iso(self.clock()))
        self.update()
        folder = dataops.export_all(self.ctx)
        names = {p.name for p in folder.iterdir()}
        self.assertLessEqual({"trades.csv", "performance_snapshots.csv", "comparison_daily.csv",
                              "ai_decisions.csv", "news_items.csv", "settings.json"}, names)
        with open(folder / "trades.csv", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["account"], "AI")
        self.assertIn("ts_local", rows[0])
        self.assertEqual(rows[0]["cash_delta"][0], "-")  # numbers stay numbers
        text = (folder / "news_items.csv").read_text(encoding="utf-8-sig")
        self.assertIn("'=HYPERLINK", text)

    def test_backup_is_complete_and_restorable(self):
        self.update()
        path = dataops.backup_now(self.ctx)
        copy = sqlite3.connect(path)
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
        copy.close()

    def test_auto_backup_once_a_day(self):
        self.assertIsNotNone(dataops.auto_backup(self.ctx))
        self.assertIsNone(dataops.auto_backup(self.ctx))

    def test_reset_clears_history_keeps_setup_and_new_balance_applies(self):
        self.update()
        dataops.reset_experiment(self.ctx, "25000")
        conn = self.ctx.db.conn()
        self.assertEqual(self.trades(), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0], 1)
        s = load_settings(self.ctx.db)
        self.assertEqual(s.start, D("25000"))
        self.assertEqual(load_state(conn, "AI", s).cash, D("25000"))
        with self.assertRaises(sqlite3.IntegrityError):  # protection is back on
            service.update_once(self.ctx, force=True)
            conn.execute("DELETE FROM trades")

    def test_starting_balance_locked_once_trades_exist(self):
        self.update()
        with self.assertRaisesRegex(ValueError, "locked"):
            self.configure(starting_balance="99999")

    def test_settings_changes_are_audited(self):
        self.configure(reset_period="weekly")
        row = self.ctx.db.conn().execute(
            "SELECT * FROM settings_audit WHERE key = 'reset_period'").fetchone()
        self.assertEqual(json.loads(row["new_value"]), "weekly")


class Settings(AppCase):
    def test_validation_messages(self):
        for changes, text in (({"starting_balance": "-1"}, "greater than zero"),
                              ({"max_exposure_pct": "150"}, "above 100"),
                              ({"reset_period": "monthly"}, "daily or weekly"),
                              ({"min_confidence": "2"}, "between 0 and 1"),
                              ({"max_trade_size": "abc"}, "must be a number")):
            with self.assertRaisesRegex(ValueError, text):
                self.configure(**changes)


class DemoMode(unittest.TestCase):
    def test_seed_runs_real_paths_and_stays_in_demo_file(self):
        folder = Path(tempfile.mkdtemp(prefix="paper_trader_demo_"))
        try:
            ctx = AppContext(get_paths(True, folder))
            seed_demo(ctx)
            conn = ctx.db.conn()
            s = load_settings(ctx.db)
            self.assertIn("FAKE", ctx.paths.db.name)
            self.assertGreater(len(load_snapshots(conn, "AI")), 30)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotes WHERE is_demo=0").fetchone()[0], 0)
            sources = {r["source"] for r in conn.execute("SELECT DISTINCT source FROM news_items")}
            self.assertTrue(all("fake" in x.lower() for x in sources))
            for account in ("MY", "AI"):
                self.assertGreaterEqual(load_state(conn, account, s).cash, Decimal(0))
            ctx.db.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_seed_refuses_real_database(self):
        folder = Path(tempfile.mkdtemp(prefix="paper_trader_demo_"))
        try:
            ctx = AppContext(get_paths(False, folder))
            with self.assertRaises(RuntimeError):
                seed_demo(ctx)
            ctx.db.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class NoKeys(unittest.TestCase):
    def test_no_key_means_unavailable_not_invented(self):
        folder = Path(tempfile.mkdtemp(prefix="paper_trader_nokey_"))
        try:
            with mock.patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": ""}):
                ctx = AppContext(get_paths(False, folder))
                s = ctx.settings()
                s.market_provider = "alphavantage"
                save_settings(ctx.db, s, ctx.clock)
                ctx.db.conn().execute(
                    "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('RY.TO', '', 't')")
                ctx.db.conn().execute("DELETE FROM news_sources")
                service.update_once(ctx, force=True)
                row = ctx.db.conn().execute("SELECT * FROM quotes").fetchone()
                self.assertEqual(row["status"], "unavailable")
                self.assertIsNone(row["price"])
                self.assertIn("ALPHAVANTAGE_API_KEY", row["error"])
                ctx.db.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_dotenv_does_not_override_real_environment(self):
        folder = Path(tempfile.mkdtemp(prefix="paper_trader_env_"))
        try:
            self.assertEqual(KEYS_FILE.parent.name, "source API")
            path = folder / "api_keys.env"
            path.write_text("# comment\nNEW_KEY_X=abc\nEXISTING_KEY_X=fromfile\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"EXISTING_KEY_X": "real"}):
                load_api_keys(path)
                import os
                self.assertEqual(os.environ["NEW_KEY_X"], "abc")
                self.assertEqual(os.environ["EXISTING_KEY_X"], "real")
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class NoBrokerage(unittest.TestCase):
    def test_source_has_no_order_routing_endpoints(self):
        """A coarse tripwire: no brokerage SDKs or order endpoints anywhere in the code."""
        root = Path(__file__).parent.parent / "paper_trader"
        banned = ("alpaca", "ib_insync", "questrade", "wealthsimple", "robin_stocks", "interactivebrokers",
                  "tdameritrade", "/orders", "place_order", "submit_order")
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, f"{word} in {path}")


if __name__ == "__main__":
    unittest.main()


class SectorNews(AppCase):
    def add_news(self, ticker, label, n):
        now = self.ctx.clock()
        for i in range(n):
            self.ctx.db.conn().execute(
                "INSERT INTO news_items (title, source, published_ts, fetched_ts, url, tickers,"
                " labels, keywords, summary, is_demo) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (f"{ticker} news {i}", "src", to_iso(now - timedelta(hours=1)), to_iso(now),
                 f"https://x.test/{ticker}/{i}", json.dumps([ticker]), json.dumps([label]),
                 "[]", ""))

    def test_same_sector_headlines_vote_at_half_weight(self):
        self.ctx.db.conn().execute(
            "INSERT INTO watchlist(ticker, name, added_ts) VALUES ('TD.TO', 'TD Bank', 't')")
        self.add_news("TD.TO", "analyst_downgrade", 3)
        self.add_news("RY.TO", "analyst_upgrade", 1)
        found = Cycle(self.ctx).signals("RY.TO")
        self.assertEqual(tally(found).label(), "Negative (1/1.5, -0.5)")
        self.update()
        self.assertEqual(self.trades(), [])  # the sector's bad news outvotes RY's one good item
        self.assertIn("Negative (1/1.5, -0.5)", self.decisions()[0]["explanation"])


class AiPlans(AppCase):
    def plans(self):
        return self.ctx.db.conn().execute("SELECT * FROM ai_plans").fetchall()

    def test_buy_saves_a_plan_and_a_sale_removes_it(self):
        self.update()
        self.assertEqual(len(self.trades()), 1)
        self.assertIn("Plan: Hold (", self.trades()[0]["reason"])
        (plan,) = self.plans()
        self.assertEqual(plan["ticker"], "RY.TO")
        self.assertTrue(plan["plan"].startswith("Hold"))
        self.assertEqual(json.loads(self.decisions()[0]["data_sources"])["plan"]["plan"],
                         plan["plan"])
        self.market.prices["RY.TO"] = "100"  # below the stop
        self.clock.advance(minutes=31)
        self.update()
        self.assertEqual([t["side"] for t in self.trades()], ["BUY", "SELL"])
        self.assertEqual(self.plans(), [])

    def test_reset_clears_plans(self):
        self.update()
        dataops.reset_experiment(self.ctx)
        self.assertEqual(self.plans(), [])

    def test_buying_more_is_not_blocked_by_the_position_count(self):
        self.configure(max_positions=1)
        self.update()
        quote = self.ctx.db.conn().execute("SELECT * FROM quotes WHERE ticker='RY.TO'").fetchone()
        cycle = Cycle(self.ctx)
        qty, why_not = cycle._size("RY.TO", quote)
        self.assertIsNone(why_not)
        self.assertGreater(qty, 0)
        self.assertIn("maximum positions", cycle._size("TD.TO", quote)[1])
