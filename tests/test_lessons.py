"""The AI learning log: results, news matching, scorecards and the text file."""
import csv
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from paper_trader import dataops, service
from paper_trader.ai.lessons import (MIN_SAMPLE, NOT_ENOUGH, build_lessons, load_finished_trades,
                                     report_text, write_lessons_file)
from paper_trader.config import get_paths
from paper_trader.context import AppContext
from paper_trader.demo_seed import seed_demo
from paper_trader.money import D
from paper_trader.news.base import NewsItem
from paper_trader.news.signals import label_text, load_categories, seed_categories, store_item
from paper_trader.settings import load_settings
from paper_trader.timeutil import to_iso
from paper_trader.valuation import load_state

from .helpers import DbTestCase


class Base(DbTestCase):
    def setUp(self):
        super().setUp()
        seed_categories(self.db.conn())
        self.settings(ai_enabled=True, default_category="zero", max_daily_buys=999,
                      max_trade_size="100000", max_exposure_pct="100", max_positions=99,
                      slippage_categories={"zero": {"mode": "abs", "value": "0"}})
        self.n = 0

    def round_trip(self, ticker="RY.TO", buy="100", sell="110", qty=5, exit_reason="Trend exit: x"):
        self.quote(ticker, buy)
        self.buy(ticker, qty, "AI", reason="Price is above its averages.")
        self.clock.advance(days=3)
        self.quote(ticker, sell)
        self.sell(ticker, qty, "AI", reason=exit_reason)
        self.clock.advance(days=1)

    def news(self, title, hours_ago=1, watch=None, tickers=()):
        self.n += 1
        conn = self.db.conn()
        item = NewsItem(title, "Wire", to_iso(self.clock() - timedelta(hours=hours_ago)),
                        f"https://x.test/{self.n}", "", tuple(tickers))
        store_item(conn, item, load_categories(conn), watch or {}, to_iso(self.clock()))

    def lessons(self):
        return build_lessons(self.db.conn(), 48)


class Results(Base):
    def test_gain_and_loss_with_holding_time(self):
        self.round_trip(sell="110")
        self.round_trip(sell="90", exit_reason="Stop-loss: x")
        win, loss = load_finished_trades(self.db.conn())[0]
        self.assertEqual((win.gain, win.gain_pct, win.won, win.days_held), (D("50"), D("10"), True, 3))
        self.assertEqual((loss.gain, loss.won, loss.exit_kind), (D("-50"), False, "Stop-loss"))
        self.assertEqual(win.exit_kind, "Trend exit")

    def test_open_position_is_not_scored(self):
        self.quote("RY.TO", "100")
        self.buy("RY.TO", 5, "AI")
        lessons = self.lessons()
        self.assertEqual((lessons.finished, lessons.still_held), ([], ["RY.TO"]))

    def test_partial_sells_and_adds_make_one_trade(self):
        self.quote("RY.TO", "100")
        self.buy("RY.TO", 5, "AI")
        self.buy("RY.TO", 5, "AI")
        self.quote("RY.TO", "120")
        self.sell("RY.TO", 4, "AI")
        self.assertEqual(load_finished_trades(self.db.conn())[0], [])
        self.sell("RY.TO", 6, "AI")
        trades = load_finished_trades(self.db.conn())[0]
        self.assertEqual(len(trades), 1)
        self.assertEqual((trades[0].cost, trades[0].proceeds, trades[0].quantity),
                         (D("1000"), D("1200"), D("10")))

    def test_my_account_is_never_included(self):
        self.quote("RY.TO", "100")
        self.buy("RY.TO", 5, "MY")
        self.sell("RY.TO", 5, "MY")
        self.engine.import_holding("RY.TO", 5, "50")
        self.assertEqual(load_finished_trades(self.db.conn()), ([], []))

    def test_results_add_up_to_realized_gain(self):
        self.round_trip(sell="112")
        self.round_trip(sell="93")
        state = load_state(self.db.conn(), "AI", load_settings(self.db))
        self.assertEqual(state.realized, self.lessons().overall.total_gain)


class NewsMatching(Base):
    def test_neutral_market_wide_news_is_included(self):
        self.news("War escalates as gold climbs")
        self.round_trip()
        trade = self.lessons().finished[0]
        self.assertTrue(trade.news[0].market_wide)
        self.assertEqual(set(trade.news[0].labels), {"geopolitical", "gold"})

    def test_only_news_before_the_buy_within_the_window(self):
        self.news("Old war story", hours_ago=100)
        self.news("Future gold story", hours_ago=-5)
        self.round_trip()
        self.assertEqual(self.lessons().finished[0].news, [])

    def test_other_tickers_news_is_excluded_own_included(self):
        self.news("TD beats estimates", tickers=["TD.TO"])
        self.news("Royal Bank beats estimates", tickers=["RY.TO"])
        self.round_trip()
        news = self.lessons().finished[0].news
        self.assertEqual([n.title for n in news], ["Royal Bank beats estimates"])
        self.assertFalse(news[0].market_wide)

    def test_unlabelled_news_is_ignored(self):
        self.news("A quiet day at the office")
        self.round_trip()
        self.assertEqual(self.lessons().finished[0].news, [])


class Scorecard(Base):
    def test_keyword_compared_with_all_trades(self):
        self.news("War in the region")
        self.round_trip(sell="90")      # -10% with war news
        self.round_trip(sell="110")     # +10% without
        lessons = self.lessons()
        self.assertEqual(lessons.overall.average_pct, D("0"))
        event = next(s for s in lessons.signals if (s.kind, s.name) == ("Event", "geopolitical"))
        word = next(s for s in lessons.signals if (s.kind, s.name) == ("Keyword", "war"))
        self.assertEqual((event.trades, event.wins, event.average_pct), (1, 0, D("-10")))
        self.assertEqual(event.versus_all, D("-10"))
        self.assertEqual(word.trades, 1)

    def test_small_samples_say_so(self):
        self.news("Gold rally")
        self.round_trip()
        self.assertEqual(self.lessons().signals[0].verdict, NOT_ENOUGH)

    def test_enough_trades_is_only_a_possible_pattern(self):
        for _ in range(MIN_SAMPLE):
            self.news("Gold rally")
            self.round_trip()
        score = next(s for s in self.lessons().signals if s.name == "gold" and s.kind == "Event")
        self.assertEqual(score.trades, MIN_SAMPLE)
        self.assertEqual(score.verdict, "possible pattern")

    def test_months_and_exits(self):
        self.round_trip(exit_reason="Stop-loss: x", sell="90")
        self.clock.advance(days=40)
        self.round_trip()
        lessons = self.lessons()
        self.assertEqual(len(lessons.months), 2)
        self.assertEqual({s.name for s in lessons.exits}, {"Stop-loss", "Trend exit"})

    def test_report_reads_plainly_and_warns(self):
        self.news("War in the region")
        self.round_trip(sell="90", exit_reason="Stop-loss: price fell.")
        text = report_text(self.lessons(), to_iso(self.clock()), False)
        for expected in ("PAPER TRADING", "patterns, not causes", "Why it sold: Stop-loss",
                         "geopolitical", "market-wide, unverified", NOT_ENOUGH):
            self.assertIn(expected, text)
        self.assertNotIn("DEMO", text)

    def test_empty_report(self):
        self.assertIn("No finished AI trades yet", report_text(self.lessons(), to_iso(self.clock()), False))


class Categories(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="paper_trader_lessons_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_keywords_use_whole_words(self):
        ctx = AppContext(get_paths(True, self.dir))
        cats = load_categories(ctx.db.conn())
        self.assertIn("gold", label_text("Gold hits a record", cats)[0])
        self.assertEqual(label_text("Goldman hires an analyst", cats)[0], [])
        self.assertIn("politics", label_text("Election called in Canada", cats)[0])
        self.assertIn("geopolitical", label_text("War fears grow", cats)[0])
        ctx.db.close()

    def test_older_database_gets_them_once_and_removal_sticks(self):
        ctx = AppContext(get_paths(False, self.dir))
        conn = ctx.db.conn()
        conn.execute("DELETE FROM signal_categories WHERE name IN ('gold', 'politics')")
        conn.execute("DELETE FROM meta WHERE key = 'signal_categories_version'")
        ctx.db.close()
        ctx = AppContext(get_paths(False, self.dir))
        names = {r["name"] for r in ctx.db.conn().execute("SELECT name FROM signal_categories")}
        self.assertLessEqual({"gold", "politics"}, names)
        ctx.db.conn().execute("DELETE FROM signal_categories WHERE name = 'gold'")
        ctx.db.close()
        ctx = AppContext(get_paths(False, self.dir))
        names = {r["name"] for r in ctx.db.conn().execute("SELECT name FROM signal_categories")}
        self.assertNotIn("gold", names)
        ctx.db.close()


class TextFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="paper_trader_lessons_"))
        self.ctx = AppContext(get_paths(True, self.dir))

    def tearDown(self):
        self.ctx.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_demo_history_produces_a_labelled_file_and_export(self):
        seed_demo(self.ctx)
        path = Path(write_lessons_file(self.ctx))
        self.assertEqual(path.name, "ai_lessons_DEMO.txt")
        text = path.read_text(encoding="utf-8")
        self.assertIn("FAKE DATA", text)
        self.assertIn("Why it bought", text)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())
        folder = dataops.export_all(self.ctx)
        self.assertTrue((folder / "ai_lessons.txt").exists())
        with open(folder / "ai_trade_results.csv", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertGreater(len(rows), 0)
        self.assertIn("gain_cad", rows[0])

    def test_update_writes_the_file_and_survives_a_locked_file(self):
        self.ctx.market = lambda: mock.Mock(min_call_seconds=0, end_of_day=False)
        with mock.patch.object(service, "refresh_market"), mock.patch.object(service, "refresh_news"):
            service.update_once(self.ctx, force=True)
            self.assertTrue(self.ctx.paths.lessons.exists())
            with mock.patch.object(service, "write_lessons_file", side_effect=PermissionError("locked")):
                service.update_once(self.ctx, force=True)
        row = self.ctx.db.conn().execute(
            "SELECT detail FROM update_log WHERE kind = 'lessons'").fetchone()
        self.assertIn("locked", row["detail"])

    def test_the_ai_cannot_read_the_lessons(self):
        root = Path(service.__file__).parent / "ai"
        for name in ("runner.py", "strategy.py", "llm_review.py"):
            self.assertNotIn("lessons", (root / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
