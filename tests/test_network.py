"""1.3.0: sectors, market links, and all-time gain by ticker."""
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from paper_trader import network
from paper_trader.ai.runner import Cycle
from paper_trader.config import get_paths
from paper_trader.context import AppContext
from paper_trader.engine import TradeRequest
from paper_trader.market.base import MarketDataProvider, Quote
from paper_trader.money import D
from paper_trader.network import (LINKS, UNCLASSIFIED, default_sector, link_direction,
                                  link_signals, net_effect, recent_hits, sector_for, sector_map,
                                  set_sector)
from paper_trader.settings import load_settings, save_settings
from paper_trader.timeutil import to_iso
from paper_trader.valuation import ticker_totals

from .helpers import FakeClock

LINK = {link.id: link for link in LINKS}


class Links(unittest.TestCase):
    def test_thirty_links_with_known_sectors(self):
        self.assertEqual([x.id for x in LINKS], list(range(1, 31)))
        allowed = set(network.SECTORS) | {network.COMPANY}
        for link in LINKS:
            self.assertLessEqual(set(link.up) | set(link.down), allowed, link.name)
            self.assertFalse(set(link.up) & set(link.down), link.name)

    def test_price_links_read_the_direction(self):
        oil = LINK[1]
        self.assertEqual(link_direction(oil, "Oil prices surge as supply tightens"), 1)
        self.assertEqual(link_direction(oil, "Crude slumps on demand worries"), -1)
        self.assertEqual(link_direction(oil, "Oil prices steady ahead of OPEC meeting"), 0)
        self.assertEqual(link_direction(oil, "Palm oil prices jump"), 0)

    def test_war_ignores_trade_and_price_wars(self):
        war = LINK[7]
        self.assertEqual(link_direction(war, "Russia launches invasion of neighbour"), 1)
        self.assertEqual(link_direction(war, "Trade war hits exporters"), 0)
        self.assertEqual(link_direction(war, "Grocers start a price war"), 0)

    def test_need_words_are_required(self):
        opec = LINK[4]
        self.assertEqual(link_direction(opec, "OPEC+ agrees output cut"), 1)
        self.assertEqual(link_direction(opec, "OPEC meets in Vienna"), 0)

    def test_a_fall_flips_the_sectors(self):
        hit = network.Hit(LINK[1], -1, 1, "Oil falls", "s", "t", "u", "", ())
        self.assertEqual(network.effect_on(hit, "CNQ.TO", "Oil & gas producers"), -1)
        self.assertEqual(network.effect_on(hit, "AC.TO", "Airlines & travel"), 1)
        self.assertEqual(network.effect_on(hit, "RY.TO", "Banks"), 0)

    def test_company_links_need_the_company_in_the_headline(self):
        hit = network.Hit(LINK[25], 1, 1, "Nvidia scandal", "s", "t", "u", "", ("NVDA.TO",))
        self.assertEqual(network.effect_on(hit, "NVDA.TO", "AI chips & semiconductors"), -1)
        self.assertEqual(network.effect_on(hit, "AMD.TO", "AI chips & semiconductors"), 0)

    def test_each_link_votes_once_and_only_the_winning_side_becomes_signals(self):
        h = lambda link, d, i: network.Hit(LINK[link], d, i, f"h{i}", "s", "t", "u", "", ())
        hits = [h(1, 1, 1), h(1, 1, 2), h(1, 1, 3), h(7, 1, 4), h(16, 1, 5)]
        # Oil up (+1, three headlines count once), war (+1), recession (-1) for an oil producer.
        net, used = net_effect(hits, "CNQ.TO", "Oil & gas producers")
        self.assertEqual(net, 1)
        signals = link_signals(hits, "CNQ.TO", "Oil & gas producers")
        self.assertEqual(sorted(s.labels[0].split(":")[0] for s in signals), ["link 1", "link 7"])
        self.assertTrue(all(s.direction == 1 for s in signals))
        self.assertEqual(link_signals([h(1, 1, 1), h(16, 1, 2)], "CNQ.TO", "Oil & gas producers"), [])


class Sectors(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(default_sector("CNQ.TO"), "Oil & gas producers")
        self.assertEqual(default_sector("GOOG.NE"), "Big tech & cloud")
        self.assertEqual(default_sector("ZZZZ.TO"), UNCLASSIFIED)
        for symbol, sector in network.DEFAULT_SECTORS.items():
            self.assertIn(sector, network.SECTORS, symbol)


class AppCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="paper_trader_net_"))
        self.clock = FakeClock()
        self.ctx = AppContext(get_paths(True, self.dir), self.clock)
        self.conn = self.ctx.db.conn()

    def tearDown(self):
        self.ctx.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def news(self, title, tickers=(), hours_ago=1, n=[0]):
        n[0] += 1
        self.conn.execute(
            "INSERT INTO news_items (title, source, published_ts, fetched_ts, url, tickers)"
            " VALUES (?, 'test', ?, ?, ?, ?)",
            (title, to_iso(self.clock() - timedelta(hours=hours_ago)), to_iso(self.clock()),
             f"https://x.test/{n[0]}", json.dumps(list(tickers))))

    def quote(self, ticker, price):
        self.conn.execute(
            "INSERT OR REPLACE INTO quotes (ticker, price, currency, exchange, quote_ts, fetched_ts,"
            " source, status, is_demo) VALUES (?, ?, 'CAD', 'X', ?, ?, 'test', 'ok', 1)",
            (ticker, price, to_iso(self.clock()), to_iso(self.clock())))


class SectorChoices(AppCase):
    def test_your_choice_overrides_the_default(self):
        set_sector(self.conn, "CNQ.TO", "Utilities")
        chosen = sector_map(self.conn)
        self.assertEqual(sector_for("CNQ.TO", chosen), "Utilities")
        self.assertEqual(sector_for("SU.TO", chosen), "Oil & gas producers")
        with self.assertRaises(ValueError):
            set_sector(self.conn, "CNQ.TO", "Made up")


class HitsFromStoredNews(AppCase):
    def test_window_and_headline_only(self):
        self.news("Oil prices jump after pipeline attack")
        self.news("Oil prices jump again", hours_ago=100)  # outside the 48-hour window
        self.conn.execute(
            "INSERT INTO news_items (title, source, published_ts, fetched_ts, url, summary)"
            " VALUES ('Quarterly results', 'test', ?, ?, 'https://x.test/s', 'oil prices jump')",
            (to_iso(self.clock()), to_iso(self.clock())))
        hits = recent_hits(self.conn, self.clock(), 48)
        self.assertEqual([(h.link.id, h.direction) for h in hits if h.link.id == 1], [(1, 1)])
        self.assertTrue(all(h.title != "Quarterly results" for h in hits))

    def test_ai_gets_link_signals_and_can_be_switched_off(self):
        self.news("Oil prices plunge as OPEC floods the market")
        signals = Cycle(self.ctx).signals("CNQ.TO")
        self.assertEqual([(s.labels[0], s.direction) for s in signals],
                         [("link 1: Oil price ↓", -1)])
        self.assertEqual(Cycle(self.ctx).signals("AC.TO")[0].direction, 1)  # airlines gain
        s = load_settings(self.ctx.db)
        s.market_links_enabled = False
        save_settings(self.ctx.db, s, self.clock)
        self.assertEqual(Cycle(self.ctx).signals("CNQ.TO"), [])


class GainByTicker(AppCase):
    def trade(self, account, side, ticker, qty, price):
        self.quote(ticker, price)
        source = "AI" if account == "AI" else "MANUAL"
        self.ctx.engine.submit(TradeRequest(account, source, side, ticker, qty, "test"))
        self.clock.advance(minutes=1)

    def test_round_trips_add_up_and_open_shares_count(self):
        s = load_settings(self.ctx.db)
        s.ticker_categories = {"GOOG.TO": "free", "RY.TO": "free"}
        s.slippage_categories["free"] = {"mode": "abs", "value": "0"}
        save_settings(self.ctx.db, s, self.clock)
        self.trade("MY", "BUY", "GOOG.TO", 10, "50")
        self.trade("MY", "SELL", "GOOG.TO", 10, "40")   # -100
        self.trade("MY", "BUY", "GOOG.TO", 10, "30")
        self.trade("MY", "SELL", "GOOG.TO", 10, "36")   # +60
        self.trade("MY", "BUY", "RY.TO", 5, "100")
        self.quote("RY.TO", "110")                      # +50 open
        rows = {r.ticker: r for r in ticker_totals(self.conn, "MY", load_settings(self.ctx.db),
                                                    self.clock())}
        goog, ry = rows["GOOG.TO"], rows["RY.TO"]
        self.assertEqual((goog.buys, goog.sells, goog.realized, goog.held), (2, 2, D("-40"), 0))
        self.assertEqual(goog.total, D("-40"))
        self.assertEqual((ry.buys, ry.sells, ry.realized, ry.unrealized, ry.total),
                         (1, 0, Decimal(0), D("50"), D("50")))
        self.assertEqual(ticker_totals(self.conn, "AI", load_settings(self.ctx.db), self.clock()), [])


if __name__ == "__main__":
    unittest.main()
