"""The trading rules, including the loopholes the rules exist to close."""
import random
import sqlite3
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from paper_trader.engine import NeedsConfirmation, TradeRejected
from paper_trader.money import D
from paper_trader.settings import load_settings
from paper_trader.valuation import account_view, budget, load_state

from .helpers import DbTestCase, FakeClock


class Base(DbTestCase):
    def state(self, account="MY"):
        return load_state(self.db.conn(), account, load_settings(self.db))

    def view(self, account="MY"):
        return account_view(self.db.conn(), account, load_settings(self.db), self.clock())

    def rejected(self, code, fn, *args, **kwargs):
        with self.assertRaises(TradeRejected) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception


class CashRules(Base):
    def setUp(self):
        super().setUp()
        self.settings(default_category="zero", slippage_categories={
            "zero": {"mode": "abs", "value": "0"}})
        self.quote("RY.TO", "100")

    def test_spec_example_4000_of_10000_leaves_6000(self):
        self.buy(qty=40)
        self.assertEqual(self.state().cash, D("6000"))
        self.assertEqual(budget(self.db.conn(), "MY", load_settings(self.db), self.clock()).buying_power, D("6000"))
        self.rejected("NO_CASH", self.buy, qty=61)
        self.buy(qty=60)
        self.assertEqual(self.state().cash, D("0"))

    def test_cannot_sell_more_than_held(self):
        self.buy(qty=10)
        self.rejected("OVERSELL", self.sell, qty=11)
        self.quote("TD.TO", "100")
        self.rejected("OVERSELL", self.sell, "TD.TO", 1)

    def test_sale_proceeds_can_be_reused(self):
        self.buy(qty=100)
        self.sell(qty=100)
        self.assertEqual(self.state().cash, D("10000"))
        self.buy(qty=100)
        self.assertEqual(self.state().positions["RY.TO"].quantity, D("100"))

    def test_average_cost_and_realized_gain(self):
        self.buy(qty=10)
        self.quote("RY.TO", "120")
        self.buy(qty=10)
        pos = self.state().positions["RY.TO"]
        self.assertEqual(pos.avg_cost, D("110"))
        self.quote("RY.TO", "130")
        self.sell(qty=5)
        state = self.state()
        self.assertEqual(state.realized, D("100"))
        self.assertEqual(state.positions["RY.TO"].avg_cost, D("110"))
        view = self.view()
        self.assertEqual(view.unrealized, D("300"))
        self.assertEqual(view.total_value, D("10400"))

    def test_invalid_inputs(self):
        self.rejected("BAD_QUANTITY", self.buy, qty=0)
        self.rejected("BAD_QUANTITY", self.buy, qty=-5)
        self.rejected("BAD_QUANTITY", self.buy, qty="1.5")
        self.rejected("BAD_INPUT", self.buy, qty="abc")
        self.rejected("BAD_INPUT", self.buy, qty="NaN")
        self.rejected("BAD_INPUT", self.buy, ticker="bad ticker!")
        self.rejected("NO_REASON", self.buy, reason="   ")

    def test_fractional_only_when_enabled(self):
        self.settings(allow_fractional=True)
        self.buy(qty="1.5")
        self.assertEqual(self.state().cash, D("9850"))
        self.rejected("BAD_QUANTITY", self.buy, qty="0.00001")


class RealizedBySale(Base):
    def setUp(self):
        super().setUp()
        self.settings(default_category="zero", slippage_categories={
            "zero": {"mode": "abs", "value": "0"}})

    def test_each_sale_gets_its_own_result(self):
        from paper_trader.ledger import realized_by_sale
        from paper_trader.valuation import load_trades
        self.quote("RY.TO", "100")
        self.buy(qty=10)
        self.quote("RY.TO", "120")
        win = self.sell(qty=4)
        self.quote("RY.TO", "90")
        loss = self.sell(qty=6)
        self.engine.import_holding("TD.TO", 5, "50")
        self.quote("TD.TO", "40")
        imported = self.sell("TD.TO", 5, sleeve="IMPORTED")
        result = realized_by_sale(D("10000"), load_trades(self.db.conn(), "MY"))
        self.assertEqual(result, {win: (D("80"), "CAD"), loss: (D("-60"), "CAD"),
                                  imported: (D("-50"), "CAD")})
        trading = sum(v for k, (v, _) in result.items() if k != imported)
        self.assertEqual(trading, self.state().realized)


class PeriodLimit(Base):
    def setUp(self):
        super().setUp()
        self.settings(default_category="zero", slippage_categories={
            "zero": {"mode": "abs", "value": "0"}}, period_limit="2000")
        self.quote("RY.TO", "100")

    def remaining(self):
        return budget(self.db.conn(), "MY", load_settings(self.db), self.clock()).remaining

    def test_limit_blocks_within_day_and_resets_next_day(self):
        self.buy(qty=20)
        self.rejected("PERIOD_LIMIT", self.buy, qty=1)
        self.clock.advance(days=1)
        self.quote("RY.TO", "100")
        self.assertEqual(self.remaining(), D("2000"))
        self.buy(qty=20)
        self.assertEqual(self.state().cash, D("6000"))

    def test_reset_never_adds_cash(self):
        self.settings(period_limit="")
        self.buy(qty=100)  # all CAD 10,000 spent
        self.clock.advance(days=1)
        self.quote("RY.TO", "100")
        self.assertEqual(self.remaining(), D("10000"))  # allowance is back...
        self.assertEqual(self.state().cash, D("0"))     # ...but no money appeared
        self.rejected("NO_CASH", self.buy, qty=1)

    def test_same_period_sale_restores_allowance(self):
        self.buy(qty=20)
        self.sell(qty=10)
        self.assertEqual(self.remaining(), D("1000"))
        self.buy(qty=10)

    def test_selling_old_shares_cannot_lift_allowance_above_limit(self):
        self.buy(qty=20)
        self.clock.advance(days=1)
        self.quote("RY.TO", "100")
        self.sell(qty=20)  # CAD 2,000 of proceeds from yesterday's position
        self.assertEqual(self.remaining(), D("2000"))
        self.rejected("PERIOD_LIMIT", self.buy, qty=21)

    def test_weekly_reset_is_monday_local(self):
        self.settings(reset_period="weekly")
        monday = datetime(2026, 11, 2, 9, 0).astimezone()
        self.clock.now = monday
        self.quote("RY.TO", "100")
        self.buy(qty=20)
        self.clock.now = datetime(2026, 11, 8, 23, 59).astimezone()  # Sunday night
        self.quote("RY.TO", "100")
        self.rejected("PERIOD_LIMIT", self.buy, qty=1)
        self.clock.now = datetime(2026, 11, 9, 0, 1).astimezone()  # Monday morning
        self.quote("RY.TO", "100")
        self.buy(qty=20)

    def test_churning_cannot_exceed_cash(self):
        self.settings(period_limit="", starting_balance="10000")
        for _ in range(5):
            self.buy(qty=100)
            self.assertLessEqual(self.view().market_value, D("10000"))
            self.rejected("NO_CASH", self.buy, qty=1)
            self.sell(qty=100)


class CadOnly(Base):
    def setUp(self):
        super().setUp()
        self.quote("GOOG", "300")
        self.quote("GOOG.TO", "40")
        self.quote("GOOG.NE", "40")
        self.fx("1.40")

    def test_on_by_default(self):
        self.assertTrue(load_settings(self.db).cad_only)

    def test_us_listing_blocked_with_helpful_message(self):
        exc = self.rejected("CAD_ONLY", self.buy, "GOOG", 1)
        self.assertIn("GOOG.TO", str(exc))

    def test_ai_is_blocked_too(self):
        self.settings(ai_enabled=True)
        self.rejected("CAD_ONLY", self.buy, "GOOG", 1, "AI")

    def test_cad_listings_trade_including_cdrs(self):
        self.buy("GOOG.TO", 1)
        self.buy("GOOG.NE", 1)
        state = self.state()
        self.assertEqual(len(state.positions), 2)
        rows = self.db.conn().execute("SELECT currency, fx_rate FROM trades").fetchall()
        self.assertTrue(all((r["currency"], r["fx_rate"]) == ("CAD", None) for r in rows))

    def test_import_of_us_listing_blocked(self):
        self.rejected("CAD_ONLY", self.engine.import_holding, "GOOG", 1, "100")

    def test_can_be_turned_off_later(self):
        self.allow_usd()
        self.buy("GOOG", 1)
        self.engine.import_holding("GOOG", 1, "100")

    def test_existing_us_position_can_still_be_sold(self):
        self.allow_usd()
        self.buy("GOOG", 2)
        self.settings(cad_only=True)
        self.rejected("CAD_ONLY", self.buy, "GOOG", 1)
        self.sell("GOOG", 2)
        self.assertEqual(self.state().positions, {})


class EndOfDayQuotes(Base):
    def eod(self, age_days: float):
        self.quote("RY.TO", "100", age_minutes=age_days * 1440)
        self.db.conn().execute("UPDATE quotes SET end_of_day = 1")

    def test_last_close_is_usable_over_a_weekend(self):
        self.settings(ai_enabled=True)
        self.eod(3)
        self.buy(qty=1, account="AI")
        self.buy(qty=1)
        flags = [r["quote_stale"] for r in self.db.conn().execute("SELECT quote_stale FROM trades")]
        self.assertEqual(flags, [0, 0])

    def test_an_old_close_is_stale(self):
        self.settings(ai_enabled=True)
        self.eod(5)
        self.rejected("STALE_QUOTE", self.buy, qty=1, account="AI")
        with self.assertRaises(NeedsConfirmation):
            self.buy(qty=1)

    def test_preview_says_it_is_a_closing_price(self):
        self.eod(1)
        self.assertTrue(self.engine.preview(self.req("BUY", "RY.TO", 1)).end_of_day)


class SlippageAndCurrency(Base):
    def setUp(self):
        super().setUp()
        self.allow_usd()

    def test_fill_is_worse_than_reference_on_both_sides(self):
        self.quote("RY.TO", "100")
        plan = self.engine.preview(self.req("BUY", "RY.TO", 10))
        self.assertEqual(plan.fill_price, D("100.03"))
        self.assertEqual(plan.reference_price, D("100"))
        self.buy(qty=10)
        plan = self.engine.preview(self.req("SELL", "RY.TO", 10))
        self.assertEqual(plan.fill_price, D("99.97"))

    def test_round_trip_loses_money(self):
        self.quote("RY.TO", "100")
        self.buy(qty=10)
        self.sell(qty=10)
        self.assertEqual(self.state().cash, D("9999.40"))
        self.assertEqual(self.state().realized, D("-0.60"))

    def test_slippage_by_ticker_category_and_percent(self):
        self.settings(ticker_categories={"XYZ.V": "illiquid", "RY.TO": "liquid"})
        self.quote("XYZ.V", "10")
        self.quote("RY.TO", "10")
        self.assertEqual(self.engine.preview(self.req("BUY", "XYZ.V", 1)).fill_price, D("10.025"))
        self.assertEqual(self.engine.preview(self.req("BUY", "RY.TO", 1)).fill_price, D("10.02"))

    def test_sell_fill_stays_positive(self):
        self.quote("RY.TO", "0.01")
        self.buy(qty=1)
        self.assertEqual(self.engine.preview(self.req("SELL", "RY.TO", 1)).fill_price, D("0.0001"))

    def test_usd_uses_recorded_rate_against_cad_cash(self):
        self.fx("1.40")
        self.quote("AAPL", "100")
        plan = self.engine.preview(self.req("BUY", "AAPL", 10))
        self.assertEqual(plan.currency, "USD")
        self.assertEqual(plan.fx_rate, D("1.40"))
        self.assertEqual(plan.cash_delta, D("-1400.42"))
        self.buy("AAPL", 10)
        self.assertEqual(self.state().cash, D("8599.58"))
        row = self.db.conn().execute("SELECT * FROM trades").fetchone()
        self.assertEqual((row["fx_rate"], row["currency"]), ("1.40", "USD"))
        self.assertIsNotNone(row["fx_date"])

    def test_fx_move_shows_in_cad_gain(self):
        self.settings(default_category="zero", slippage_categories={
            "zero": {"mode": "abs", "value": "0"}})
        self.fx("1.40")
        self.quote("AAPL", "100")
        self.buy("AAPL", 10)
        self.clock.advance(hours=1)
        self.fx("1.50", "2026-11-05")
        self.quote("AAPL", "100")
        self.assertEqual(self.view().unrealized, D("100"))

    def test_usd_blocked_without_recent_fx(self):
        self.quote("AAPL", "100")
        self.rejected("NO_FX", self.buy, "AAPL", 1)
        self.fx("1.40", "2026-01-01")
        self.rejected("NO_FX", self.buy, "AAPL", 1)

    def test_currency_mismatch_blocked(self):
        self.quote("RY.TO", "100", currency="USD")
        self.rejected("BAD_CURRENCY", self.buy, qty=1)


class MissingData(Base):
    def test_no_price_blocks_trade(self):
        self.rejected("NO_PRICE", self.buy, qty=1)
        self.quote("RY.TO", None, status="unavailable", error="rate limited")
        exc = self.rejected("NO_PRICE", self.buy, qty=1)
        self.assertIn("rate limited", str(exc))

    def test_unavailable_with_last_price_blocks_trade(self):
        self.quote("RY.TO", "100", status="unavailable")
        self.rejected("NO_PRICE", self.buy, qty=1)

    def test_stale_quote_needs_confirmation_and_is_flagged(self):
        self.quote("RY.TO", "100", age_minutes=600)
        with self.assertRaises(NeedsConfirmation):
            self.buy(qty=1)
        self.buy(qty=1, confirm_stale=True)
        row = self.db.conn().execute("SELECT quote_stale FROM trades").fetchone()
        self.assertEqual(row["quote_stale"], 1)

    def test_valuation_never_invents_a_price(self):
        self.quote("RY.TO", "100")
        self.buy(qty=10)
        self.db.conn().execute("DELETE FROM quotes")
        view = self.view()
        self.assertEqual(view.data_quality, "partial")
        self.assertEqual(view.missing, ["RY.TO"])
        self.assertIsNone(view.holdings[0].price)
        self.assertIsNone(view.holdings[0].market_value)
        self.assertEqual(view.total_value, view.starting + view.realized + view.unrealized)

    def test_demo_price_cannot_be_used_in_real_mode(self):
        self.engine.demo = False
        self.quote("RY.TO", "100", is_demo=True)
        self.rejected("DEMO_MISMATCH", self.buy, qty=1)


class Duplicates(Base):
    def setUp(self):
        super().setUp()
        self.quote("RY.TO", "100")

    def test_same_client_id_recorded_once(self):
        self.buy(qty=1, client_id="abc")
        self.rejected("DUPLICATE", self.buy, qty=1, client_id="abc")
        self.assertEqual(self.state().positions["RY.TO"].quantity, D("1"))

    def test_identical_quick_trade_needs_confirmation(self):
        self.buy(qty=1, confirm_duplicate=False)
        with self.assertRaises(NeedsConfirmation):
            self.buy(qty=1, confirm_duplicate=False)
        self.clock.advance(seconds=11)
        self.quote("RY.TO", "100")
        self.buy(qty=1, confirm_duplicate=False)


class ImportedHoldings(Base):
    def setUp(self):
        super().setUp()
        self.settings(default_category="zero", slippage_categories={
            "zero": {"mode": "abs", "value": "0"}})
        self.quote("RY.TO", "100")

    def test_import_does_not_change_cash_or_account_value(self):
        before = self.view().total_value
        self.engine.import_holding("RY.TO", 1000, "50")
        self.assertEqual(self.view().total_value, before)
        self.assertEqual(self.state().cash, D("10000"))
        self.assertEqual(self.state().imported["RY.TO"].quantity, D("1000"))

    def test_selling_imported_shares_creates_no_buying_power(self):
        self.engine.import_holding("RY.TO", 1000, "50")
        self.sell(qty=1000, sleeve="IMPORTED")
        state = self.state()
        self.assertEqual(state.cash, D("10000"))
        self.assertEqual(state.imported_proceeds["CAD"], D("100000"))
        self.assertEqual(state.imported_realized["CAD"], D("50000"))
        self.assertEqual(state.realized, D("0"))
        self.rejected("NO_CASH", self.buy, qty=101)

    def test_imported_shares_cannot_be_sold_as_trading_shares(self):
        self.engine.import_holding("RY.TO", 10, "50")
        self.rejected("OVERSELL", self.sell, qty=10)

    def test_trading_shares_cannot_be_sold_as_imported(self):
        self.buy(qty=10)
        self.rejected("OVERSELL", self.sell, qty=10, sleeve="IMPORTED")

    def test_imported_validation(self):
        self.rejected("BAD_QUANTITY", self.engine.import_holding, "RY.TO", 0, "50")
        self.rejected("BAD_PRICE", self.engine.import_holding, "RY.TO", 1, "0")
        with self.assertRaises(ValueError):
            self.engine.import_holding("RY.TO", 1, "x")


class AccountSeparation(Base):
    def setUp(self):
        super().setUp()
        self.quote("RY.TO", "100")

    def test_engine_refuses_cross_account_requests(self):
        req = self.req("BUY", "RY.TO", 1, "MY")
        req.source = "AI"
        self.rejected("WRONG_ACCOUNT", self.engine.submit, req)
        req = self.req("BUY", "RY.TO", 1, "AI")
        req.source = "MANUAL"
        self.rejected("WRONG_ACCOUNT", self.engine.submit, req)

    def test_database_refuses_an_ai_row_in_my_account(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn().execute(
                "INSERT INTO trades (client_id, ts, account, source, side, sleeve, ticker, currency,"
                " quantity, fill_price, gross_native, cash_delta, reason)"
                " VALUES ('x', 't', 'MY', 'AI', 'BUY', 'TRADING', 'RY.TO', 'CAD', '1', '1', '1',"
                " '-1', 'r')")

    def test_trades_are_append_only(self):
        self.buy(qty=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn().execute("UPDATE trades SET quantity = '999'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn().execute("DELETE FROM trades")


class AiLimits(Base):
    def setUp(self):
        super().setUp()
        self.settings(ai_enabled=True, default_category="zero", slippage_categories={
            "zero": {"mode": "abs", "value": "0"}}, max_trade_size="1000", max_daily_buys=2,
            max_positions=2, max_exposure_pct="15")
        for ticker in ("RY.TO", "TD.TO", "BNS.TO"):
            self.quote(ticker, "100")

    def test_off_switch_blocks_ai_trades(self):
        self.settings(ai_enabled=False)
        self.rejected("AI_OFF", self.buy, qty=1, account="AI")

    def test_max_trade_size(self):
        self.rejected("AI_MAX_TRADE", self.buy, qty=11, account="AI")
        self.buy(qty=10, account="AI")

    def test_max_daily_buys(self):
        self.settings(max_exposure_pct="100", max_positions=5)
        self.buy("RY.TO", 1, "AI")
        self.buy("TD.TO", 1, "AI")
        self.rejected("AI_MAX_BUYS", self.buy, "BNS.TO", 1, "AI")

    def test_max_positions(self):
        self.settings(max_exposure_pct="100", max_daily_buys=9)
        self.buy("RY.TO", 1, "AI")
        self.buy("TD.TO", 1, "AI")
        self.rejected("AI_MAX_POSITIONS", self.buy, "BNS.TO", 1, "AI")
        self.buy("RY.TO", 1, "AI")

    def test_max_exposure(self):
        self.buy("RY.TO", 10, "AI")
        self.rejected("AI_MAX_EXPOSURE", self.buy, "TD.TO", 6, "AI")

    def test_ai_needs_fresh_quote(self):
        self.quote("RY.TO", "100", age_minutes=600)
        self.rejected("STALE_QUOTE", self.buy, qty=1, account="AI")
        self.rejected("STALE_QUOTE", self.engine.submit,
                      self.req("BUY", "RY.TO", 1, "AI", confirm_stale=True))

    def test_manual_trades_ignore_ai_only_limits(self):
        self.buy(qty=50)
        self.assertEqual(self.state("AI").cash, D("10000"))


class Properties(Base):
    def test_random_trading_never_breaks_the_accounting(self):
        """Thousands of random operations: no negative cash, identity always holds."""
        rng = random.Random(20261104)
        self.settings(period_limit="4000", allow_fractional=True, cad_only=False)
        self.fx("1.37")
        tickers = ["RY.TO", "TD.TO", "AAPL", "MSFT"]
        s = load_settings(self.db)
        for step in range(1500):
            self.clock.advance(minutes=rng.choice((1, 30, 720)))
            self.fx("1.37", self.clock().astimezone().date().isoformat())
            ticker = rng.choice(tickers)
            self.quote(ticker, f"{rng.uniform(5, 300):.4f}")
            for other in tickers:
                if other != ticker:
                    self.db.conn().execute(
                        "UPDATE quotes SET quote_ts = ? WHERE ticker = ?",
                        (self.db.conn().execute("SELECT quote_ts FROM quotes WHERE ticker = ?",
                                                (ticker,)).fetchone()[0], other))
            side = rng.choice(("BUY", "BUY", "SELL"))
            qty = f"{rng.uniform(0.01, 40):.4f}"
            try:
                self.engine.submit(self.req(side, ticker, qty))
            except TradeRejected:
                pass
            state = self.state()
            self.assertGreaterEqual(state.cash, 0)
            book = sum((p.book_cost for p in state.positions.values()), Decimal(0))
            self.assertEqual(state.cash + book, s.start + state.realized, f"step {step}")
            self.assertTrue(all(p.quantity > 0 for p in state.positions.values()))
            b = budget(self.db.conn(), "MY", s, self.clock())
            self.assertLessEqual(b.used, s.limit)
        self.assertGreater(state.trade_count, 50)


if __name__ == "__main__":
    unittest.main()
