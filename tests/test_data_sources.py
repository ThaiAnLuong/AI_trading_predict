"""Parsers, run against real responses saved from each provider (tests/fixtures)."""
import json
import unittest
from pathlib import Path

from paper_trader import http
from paper_trader.market import alphavantage, fx, twelvedata, yahoo
from paper_trader.market.base import Bar, DataUnavailable
from paper_trader.money import D
from paper_trader.news.finnhub import parse_company_news
from paper_trader.news.rss import parse_feed
from paper_trader.news.signals import DEFAULT_CATEGORIES, label_text, match_tickers
from paper_trader.symbols import currency_for, normalize_ticker

FIXTURES = Path(__file__).parent / "fixtures"
# Saved Yahoo responses stay in the private folder, so the public copy skips these tests.
needs_yahoo_files = unittest.skipUnless((FIXTURES / "yahoo_chart_bars_goog_to.json").exists(),
                                        "saved Yahoo data is not in the public copy")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))


class Symbols(unittest.TestCase):
    def test_normalize_and_currency(self):
        self.assertEqual(normalize_ticker(" ry.to "), "RY.TO")
        self.assertEqual(normalize_ticker("BRK.B"), "BRK.B")
        self.assertEqual(normalize_ticker("RCI-B.TO"), "RCI-B.TO")
        self.assertEqual(currency_for("RY.TO"), "CAD")
        self.assertEqual(currency_for("ABC.V"), "CAD")
        self.assertEqual(currency_for("GOOG.TO"), "CAD")
        self.assertEqual(currency_for("GOOG.NE"), "CAD")
        self.assertEqual(currency_for("AAPL"), "USD")
        for bad in ("", "1ABC", "A B", "TOOLONGTICKER", "AAPL;DROP"):
            with self.assertRaises(ValueError):
                normalize_ticker(bad)

    def test_provider_symbols(self):
        self.assertEqual(twelvedata.provider_symbol("RY.TO"), "RY:TSX")
        self.assertEqual(twelvedata.provider_symbol("GOOG.NE"), "GOOG:NEO")
        self.assertEqual(twelvedata.provider_symbol("AAPL"), "AAPL")
        self.assertEqual(alphavantage.provider_symbol("RY.TO"), "RY.TRT")
        self.assertEqual(alphavantage.provider_symbol("GOOG.TO"), "GOOG.TRT")
        self.assertEqual(alphavantage.provider_symbol("ABC.V"), "ABC.TRV")
        with self.assertRaises(DataUnavailable):
            alphavantage.provider_symbol("GOOG.NE")


class TwelveData(unittest.TestCase):
    def test_real_quote(self):
        q = twelvedata.parse_quote("AAPL", load("twelvedata_quote_aapl.json"))
        self.assertGreater(q.price, 0)
        self.assertEqual((q.currency, q.exchange), ("USD", "NASDAQ"))
        self.assertRegex(q.quote_ts, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")

    def test_real_series_sorted_oldest_first(self):
        bars = twelvedata.parse_series(load("twelvedata_series_aapl.json"))
        self.assertEqual(len(bars), 5)
        self.assertEqual([b.date for b in bars], sorted(b.date for b in bars))

    def test_missing_fields_raise_not_guess(self):
        with self.assertRaises(DataUnavailable):
            twelvedata.parse_quote("AAPL", {"symbol": "AAPL"})
        with self.assertRaises(DataUnavailable):
            twelvedata.parse_quote("AAPL", {"close": "abc", "timestamp": 1, "currency": "USD"})


class AlphaVantage(unittest.TestCase):
    def test_real_quote(self):
        q = alphavantage.parse_quote("IBM", load("alphavantage_quote_ibm.json"))
        self.assertGreater(q.price, 0)
        self.assertEqual(q.currency, "USD")
        self.assertTrue(q.end_of_day)

    def test_quote_from_bars_uses_latest_close_in_cad(self):
        bars = [Bar("2026-11-02", D("10"), D("5")), Bar("2026-11-03", D("11"), D("6"))]
        q = alphavantage.quote_from_bars("GOOG.TO", bars)
        self.assertEqual((q.price, q.currency, q.exchange), (D("11"), "CAD", "TSX"))
        self.assertTrue(q.end_of_day)
        self.assertTrue(q.quote_ts.startswith("2026-11-03T21:00"))
        with self.assertRaises(DataUnavailable):
            alphavantage.quote_from_bars("GOOG.TO", [])

    def test_real_demo_notice_is_unavailable(self):
        with self.assertRaises(DataUnavailable):
            alphavantage.parse_quote("SHOP.TO", load("alphavantage_demo_notice.json"))

    def test_empty_quote_is_unavailable(self):
        with self.assertRaises(DataUnavailable):
            alphavantage.parse_quote("NOPE", {"Global Quote": {}})


class Yahoo(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(yahoo.provider_symbol("GOOG.TO"), "GOOG.TO")
        self.assertEqual(yahoo.provider_symbol("GOOG.NE"), "GOOG.NE")
        self.assertEqual(yahoo.provider_symbol("ABC.V"), "ABC.V")
        self.assertEqual(yahoo.provider_symbol("RCI-B.TO"), "RCI-B.TO")
        self.assertEqual(yahoo.provider_symbol("CTC.A.TO"), "CTC-A.TO")
        self.assertEqual(yahoo.provider_symbol("BRK.B"), "BRK-B")

    @needs_yahoo_files
    def test_real_quote_is_cad_and_intraday(self):
        q = yahoo.parse_quote("GOOG.TO", load("yahoo_chart_quote_goog_to.json"))
        self.assertGreater(q.price, 0)
        self.assertEqual(q.currency, "CAD")
        self.assertFalse(q.end_of_day)
        self.assertEqual(q.price, q.price.quantize(D("0.0001")))
        self.assertRegex(q.quote_ts, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
        self.assertIn("unofficial", q.source.lower())

    @needs_yahoo_files
    def test_real_history(self):
        bars = yahoo.parse_series(load("yahoo_chart_bars_goog_to.json"))
        self.assertGreater(len(bars), 100)
        days = [b.date for b in bars]
        self.assertEqual(days, sorted(set(days)))  # one bar per day, oldest first
        self.assertTrue(all(b.close > 0 for b in bars))

    @needs_yahoo_files
    def test_gaps_are_skipped_not_filled(self):
        data = load("yahoo_chart_bars_goog_to.json")
        quote = data["chart"]["result"][0]["indicators"]["quote"][0]
        count = len(yahoo.parse_series(data))
        quote["close"][3] = None
        self.assertEqual(len(yahoo.parse_series(data)), count - 1)

    def test_error_and_missing_fields_raise_not_guess(self):
        refused = {"chart": {"result": None, "error": {"code": "Not Found", "description": "gone"}}}
        for bad in (refused, {}, {"chart": {"result": [], "error": None}}, [],
                    {"chart": {"result": [{"meta": {"currency": "CAD"}}], "error": None}}):
            with self.assertRaises(DataUnavailable):
                yahoo.parse_quote("GOOG.TO", bad)
            with self.assertRaises(DataUnavailable):
                yahoo.parse_series(bad)

    def test_no_browser_disguise(self):
        text = (Path(yahoo.__file__).read_text(encoding="utf-8")
                + Path(http.__file__).read_text(encoding="utf-8"))
        self.assertNotIn("Mozilla", text)
        self.assertNotIn("impersonate", text)
        self.assertIn("PaperTrader", http.USER_AGENT)


class Fx(unittest.TestCase):
    def test_real_rate(self):
        rate = fx.parse_fx(load("boc_fx.json"))
        self.assertGreater(rate.rate, D("1"))
        self.assertRegex(rate.rate_date, r"^\d{4}-\d\d-\d\d$")

    def test_empty_raises(self):
        with self.assertRaises(DataUnavailable):
            fx.parse_fx({"observations": []})


class Feeds(unittest.TestCase):
    def test_real_bank_of_canada_rdf(self):
        items = parse_feed((FIXTURES / "boc_press.rdf").read_bytes(), "BoC")
        self.assertGreater(len(items), 3)
        self.assertTrue(all(i.title and i.url.startswith("https://") for i in items))
        self.assertTrue(all(i.published_ts.endswith("Z") for i in items))

    def test_real_federal_reserve_rss(self):
        items = parse_feed((FIXTURES / "fed_press.xml").read_bytes(), "Fed")
        self.assertGreater(len(items), 3)
        self.assertTrue(all(i.title and i.url.startswith("https://") for i in items))

    def test_atom_and_undated_item(self):
        body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Dated &amp; good</title><link href="https://x.test/1"/>
            <updated>2026-11-04T12:00:00Z</updated><summary>&lt;b&gt;Hi&lt;/b&gt; there</summary></entry>
          <entry><title>No date</title><link href="https://x.test/2"/></entry>
        </feed>"""
        items = parse_feed(body, "Atom")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Dated & good")
        self.assertEqual(items[0].summary, "Hi there")

    def test_bad_xml(self):
        with self.assertRaises(DataUnavailable):
            parse_feed(b"<html>not a feed", "x")

    def test_finnhub_documented_format(self):
        data = [{"category": "company", "datetime": 1793800000, "headline": "Apple beats estimates",
                 "id": 1, "image": "", "related": "AAPL", "source": "Reuters", "summary": "s",
                 "url": "https://x.test/a"}, {"headline": "broken"}]
        items = parse_company_news("AAPL", data, "Finnhub")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].tickers, ("AAPL",))
        self.assertEqual(items[0].source, "Finnhub: Reuters")


class Signals(unittest.TestCase):
    cats = [(n, k, d) for n, d, e, k in DEFAULT_CATEGORIES if e]

    def test_labels_use_whole_words(self):
        self.assertEqual(label_text("Bank raises guidance", self.cats)[0], ["earnings_beat"])
        self.assertEqual(label_text("Software award", self.cats)[0], [])  # 'war' inside words

    def test_bare_symbol_is_not_a_match(self):
        watch = {"IT": "Gartner", "RY.TO": "Royal Bank"}
        self.assertEqual(match_tickers("IT spending rises", watch), [])
        self.assertEqual(match_tickers("Royal Bank results", watch), ["RY.TO"])
        self.assertEqual(match_tickers("Results (TSX: RY) today", watch), ["RY.TO"])
        self.assertEqual(match_tickers("Watching $IT closely", watch), ["IT"])


if __name__ == "__main__":
    unittest.main()
