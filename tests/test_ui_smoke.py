"""Opens the real window with demo data and drives the main flows."""
import shutil
import tempfile
import tkinter as tk
import unittest
from datetime import timedelta
from pathlib import Path
from tkinter import ttk
from unittest import mock

from paper_trader import __version__
from paper_trader.config import get_paths
from paper_trader.context import AppContext
from paper_trader.demo_seed import seed_demo
from paper_trader.money import D
from paper_trader.performance import take_snapshots
from paper_trader.ui import admin, common, dashboard, tabs
from paper_trader.ui import lessons as lessons_ui
from paper_trader.ui.app import App
from paper_trader.ui.chart import nice_ticks
from paper_trader.valuation import load_state


class Ticks(unittest.TestCase):
    def test_ticks_cover_the_range(self):
        ticks = nice_ticks(9812.0, 10377.0)
        self.assertLessEqual(ticks[0], 9812)
        self.assertGreaterEqual(ticks[-1], 10377)

    def test_a_flat_or_tiny_range_still_gives_a_usable_axis(self):
        for low, high in ((10000.0, 10000.0), (100.0, 100.0), (0.0, 0.0), (9999.99, 10000.0),
                          (10000.0, 10000.01), (-5.0, -5.0), (0.01, 0.01), (2500.0, 2500.0)):
            ticks = nice_ticks(low, high)
            self.assertGreaterEqual(len(ticks), 2, (low, high))
            self.assertLess(ticks[0], ticks[-1], (low, high))
            self.assertLessEqual(ticks[0], low, (low, high))
            self.assertGreaterEqual(ticks[-1], high, (low, high))


class FreshExperiment(unittest.TestCase):
    """The real first days: no trades, no watchlist, and both accounts flat at the start."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="paper_trader_fresh_"))
        self.ctx = AppContext(get_paths(False, self.dir))
        self.patch = mock.patch("paper_trader.service.update_once")  # no network in tests
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.ctx.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def open_window(self):
        try:
            app = App(self.ctx)
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        errors = []
        app.report_callback_exception = lambda exc, value, tb: errors.append(value)
        return app, errors

    def visit_every_tab(self, app):
        for i in range(len(app.tabs)):
            app.book.select(i)
            app.update_idletasks()
            app.update()

    def test_opens_with_no_snapshots(self):
        app, errors = self.open_window()
        self.visit_every_tab(app)
        app.close()
        self.assertEqual(errors, [])

    def test_opens_with_flat_snapshots_on_one_and_on_many_days(self):
        for days in (0, 1, 3):
            with self.subTest(days=days):
                for offset in range(days + 1):
                    self.ctx.clock.override = self.ctx.clock.__class__()() + timedelta(days=offset)
                    take_snapshots(self.ctx.db, self.ctx.clock)
                self.ctx.clock.override = None
                app, errors = self.open_window()
                self.visit_every_tab(app)
                chart = next(t for t in app.tabs if isinstance(t, tabs.CompareTab)).chart
                self.assertLess(chart.low, chart.high)
                app.close()
                self.assertEqual(errors, [])

    def press_x(self, app):
        """Run exactly what Windows runs when the window's X is clicked."""
        app.tk.call(app.protocol("WM_DELETE_WINDOW"))
        app.update()

    def test_x_minimizes_and_keeps_the_ai_running(self):
        app, _ = self.open_window()
        app.update()
        self.press_x(app)
        self.assertEqual(app.state(), "iconic")
        self.assertTrue(app.worker.is_alive())
        app.deiconify()
        app.update()
        self.assertEqual(app.state(), "normal")
        app.close()

    def test_exit_needs_confirmation(self):
        app, _ = self.open_window()
        with mock.patch("paper_trader.ui.app.messagebox.askyesno", return_value=False) as ask:
            app.exit_app()
        ask.assert_called_once()
        self.assertEqual(ask.call_args.kwargs["default"], "no")
        self.assertTrue(app.worker.is_alive())
        self.assertTrue(app.winfo_exists())
        with mock.patch("paper_trader.ui.app.messagebox.askyesno", return_value=True):
            app.exit_app()
        self.assertFalse(app.worker.is_alive())

    def test_dark_mode_is_the_default_switches_live_and_is_remembered(self):
        self.assertTrue(self.ctx.settings().dark_mode)
        app, errors = self.open_window()
        style = ttk.Style(app)
        chart = next(t for t in app.tabs if isinstance(t, tabs.CompareTab)).chart
        preview = next(t for t in app.tabs if isinstance(t, dashboard.MyAccountTab)).preview_text
        self.assertEqual(style.lookup("Treeview", "background"), common.DARK["surface"])
        self.assertEqual(chart.cget("background"), common.DARK["surface"])
        self.assertEqual(preview.cget("background"), common.DARK["panel"])

        app.dark.set(False)
        app.toggle_dark()
        self.visit_every_tab(app)
        self.assertEqual(style.lookup("Treeview", "background"), common.LIGHT["surface"])
        self.assertEqual(chart.cget("background"), common.LIGHT["surface"])
        self.assertEqual(preview.cget("background"), common.LIGHT["panel"])
        self.assertFalse(self.ctx.settings().dark_mode)
        app.close()
        self.assertEqual(errors, [])

        app, _ = self.open_window()
        self.assertFalse(app.dark.get())
        self.assertEqual(ttk.Style(app).lookup("Treeview", "background"), common.LIGHT["surface"])
        app.close()

    def test_version_is_shown(self):
        app, _ = self.open_window()
        self.assertIn(__version__, app.title())
        app.close()

    def test_one_broken_tab_does_not_stop_the_app(self):
        with mock.patch.object(tabs.CompareTab, "refresh", side_effect=ZeroDivisionError("boom")):
            app, _ = self.open_window()
            self.assertIn("boom", app.state_text)
            self.visit_every_tab(app)
            log = self.ctx.db.conn().execute(
                "SELECT detail FROM update_log WHERE kind = 'ui'").fetchone()
            self.assertIn("boom", log["detail"])
            app.close()


class UiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="paper_trader_ui_"))
        cls.ctx = AppContext(get_paths(True, cls.dir))
        seed_demo(cls.ctx)
        try:
            cls.app = App(cls.ctx)
        except tk.TclError as exc:
            raise unittest.SkipTest(f"no display: {exc}")
        cls.app.report_callback_exception = cls.record_error
        cls.errors = []

    @classmethod
    def record_error(cls, exc, value, tb):
        cls.errors.append(value)

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls.ctx.db.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def tab(self, cls):
        return next(t for t in self.app.tabs if isinstance(t, cls))

    def pump(self):
        self.app.update_idletasks()
        self.app.update()

    def test_every_tab_renders(self):
        for i, tab in enumerate(self.app.tabs):
            self.app.book.select(i)
            self.pump()
            tab.refresh()
        self.assertEqual(self.errors, [])

    def tags_by_value(self, table, column):
        tree = table.tree
        position = list(tree["columns"]).index(column)
        return [(tree.item(i, "values")[position], tree.item(i, "tags")) for i in tree.get_children()]

    def check_sign_colours(self, pairs):
        for value, tags in pairs:
            text = str(value)
            if text.startswith("+") and text.strip("+0.,CAD ") != "":
                self.assertEqual(tuple(tags), ("gain",), value)
            elif text.startswith("-"):
                self.assertEqual(tuple(tags), ("loss",), value)
            else:
                self.assertEqual(tuple(tags or ()), (), value)

    def test_holdings_rows_are_green_or_red_by_gain(self):
        for cls in (dashboard.MyAccountTab, dashboard.AiAccountTab):
            tab = self.tab(cls)
            tab.refresh()
            self.check_sign_colours(self.tags_by_value(tab.holdings, "Unrealized CAD"))
        my = self.tab(dashboard.MyAccountTab)
        pairs = self.tags_by_value(my.holdings, "Unrealized CAD")
        self.assertTrue(any(tags for _, tags in pairs), "demo should have a coloured holding")
        self.check_sign_colours(self.tags_by_value(my.imported, "Unrealized (own ccy)"))

    def test_trade_log_colours_sales_only(self):
        tab = self.tab(tabs.TradeLogTab)
        tab.refresh()
        tree = tab.table.tree
        action = list(tree["columns"]).index("Action")
        coloured = 0
        for iid in tree.get_children():
            values, tags = tree.item(iid, "values"), tree.item(iid, "tags")
            if values[action] != "SELL":
                self.assertFalse(tags, values)
            else:
                coloured += bool(tags)
        self.assertGreater(coloured, 0)
        self.check_sign_colours(self.tags_by_value(tab.table, "Realized"))

    def test_watchlist_marks_what_the_ai_holds(self):
        s = self.ctx.settings()
        s.max_daily_buys, s.max_positions = 99, 99
        from paper_trader.settings import save_settings
        save_settings(self.ctx.db, s, self.ctx.clock)
        from paper_trader.engine import TradeRejected, TradeRequest
        from paper_trader.service import refresh_one
        refresh_one(self.ctx, "DEMB.TO")
        try:
            self.ctx.engine.submit(TradeRequest("AI", "AI", "BUY", "DEMB.TO", 1, "UI test"))
        except TradeRejected as exc:
            if exc.code != "DUPLICATE":
                raise
        tab = self.tab(tabs.WatchlistTab)
        tab.refresh()
        tree = tab.table.tree
        held = list(tree["columns"]).index("AI holds")
        from paper_trader.valuation import load_state
        ai = set(load_state(self.ctx.db.conn(), "AI", self.ctx.settings()).positions)
        self.assertIn("DEMB.TO", ai)
        for iid in tree.get_children():
            expected = ("held",) if iid in ai else ()
            self.assertEqual(tuple(tree.item(iid, "tags") or ()), expected, iid)
            self.assertEqual(tree.item(iid, "values")[held], "yes" if iid in ai else "")

    def test_reference_prices_are_bold_in_every_table(self):
        my, ai = self.tab(dashboard.MyAccountTab), self.tab(dashboard.AiAccountTab)
        checks = [(my.holdings, "Ref price"), (my.imported, "Ref price"), (ai.holdings, "Ref price"),
                  (self.tab(tabs.WatchlistTab).table, "Ref price"),
                  (self.tab(tabs.TradeLogTab).table, "Ref price"),
                  (self.tab(tabs.DecisionsTab).table, "Ref price")]
        for tab in self.app.tabs:
            tab.refresh()
        seen = 0
        for table, column in checks:
            for value, _ in self.tags_by_value(table, column):
                text = str(value)
                if text in ("n/a", "-"):
                    continue
                seen += 1
                self.assertFalse(any(ch.isascii() and ch.isdigit() for ch in text), text)
                self.assertEqual(common.plain_num(text), common.plain_num(text).strip())
        self.assertGreater(seen, 0)

    def test_bold_digits_round_trip(self):
        self.assertEqual(common.plain_num(common.bold_num("1,234.50 CAD")), "1,234.50 CAD")
        self.assertNotEqual(common.bold_num("12"), "12")

    def test_ticker_list_shows_amount_owned_and_highlights_it(self):
        tab = self.tab(dashboard.MyAccountTab)
        tab.refresh()
        owned = tab.owned
        self.assertTrue(owned, "demo My account should hold something")
        values = list(tab.ticker_box["values"])
        for line in values:
            ticker = line.split()[0]
            if ticker in owned:
                self.assertTrue(line.endswith(owned[ticker]), line)
            else:
                self.assertEqual(line, ticker)
        # Picking a line keeps only the ticker.
        line = next(v for v in values if v.split()[0] in owned)
        tab.ticker.set(line)
        tab.pick_ticker()
        self.assertEqual(tab.ticker.get(), line.split()[0])
        self.assertEqual(tab.request().ticker, line.split()[0])
        # Opening the list colours the owned lines.
        tab.mark_owned()
        box = tab.listbox()
        tab.ticker_box.tk.call(box, "delete", 0, "end")
        for v in values:
            tab.ticker_box.tk.call(box, "insert", "end", v)
        self.pump()
        for i, v in enumerate(values):
            colour = str(tab.ticker_box.tk.call(box, "itemcget", i, "-background"))
            expected = common.colors()["held_bg"] if v.split()[0] in owned else ""
            self.assertEqual(colour, expected, v)

    def test_dropdown_amounts(self):
        self.assertEqual(dashboard.ticker_choice("RY.TO", None), "RY.TO")
        line = dashboard.ticker_choice("RY.TO", "12")
        self.assertEqual(len(line), dashboard.CHOICE_WIDTH)
        self.assertTrue(line.startswith("RY.TO ") and line.endswith(" 12"))

    def test_plan_rows(self):
        base = {"ts": "2026-11-10T15:00:00.000000Z", "target": "12.00", "stop": "9.00",
                "trailing": 1, "add_at": None, "why": "test", "price": "10", "currency": "CAD"}
        rows = [dict(base, ticker="A.TO", plan="Sell soon (1-3 days)"),
                dict(base, ticker="B.TO", plan="Hold (2-5 days)", trailing=0, add_at="9.50"),
                dict(base, ticker="GONE.TO", plan="Hold (2-5 days)")]
        out = {iid: (values, tag) for iid, values, tag in dashboard.plan_rows(rows, {"A.TO", "B.TO"})}
        self.assertEqual(set(out), {"A.TO", "B.TO"})  # sold tickers are not shown
        values, tag = out["A.TO"]
        self.assertEqual((values[1], values[3], values[4], tag),
                         ("Sell soon (1-3 days)", "9.00 (raised)", "not now", "loss"))
        values, tag = out["B.TO"]
        self.assertEqual((values[3], values[4], tag), ("9.00", "9.50", ""))
        self.assertEqual(common.plain_num(values[5]), "10.0000")

    def test_ai_tab_shows_plans_only_for_holdings(self):
        tab = self.tab(dashboard.AiAccountTab)
        tab.refresh()
        ai = load_state(self.ctx.db.conn(), "AI", self.ctx.settings())
        shown = set(tab.plans.tree.get_children())
        self.assertTrue(shown, "the demo AI holding should have a plan")
        self.assertLessEqual(shown, set(ai.positions))

    def test_tabs_are_grouped_and_the_buttons_switch_pages(self):
        from paper_trader.ui.app import GROUPS
        names = [[title for title, _ in pages] for _, pages in GROUPS]
        self.assertEqual(names, [["My account", "AI account"],
                                 ["Compare", "Watchlist", "Trade log"],
                                 ["AI decisions", "AI lessons", "News", "Market links"],
                                 ["Settings", "About and data"]])
        buttons = [w for w in self.all_widgets(self.app)
                   if isinstance(w, ttk.Radiobutton) and str(w.cget("style")) == "Tab.Toolbutton"]
        self.assertEqual([b.cget("text") for b in buttons], sum(names, []))
        buttons[4].invoke()  # Trade log
        self.pump()
        self.assertEqual(self.app.book.index(self.app.book.select()), 4)
        self.app.book.select(0)  # choosing a page elsewhere moves the highlight
        self.pump()
        self.assertEqual(self.app.page.get(), 0)
        self.assertEqual(self.errors, [])

    def all_widgets(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.all_widgets(child)

    def test_sector_column_in_holdings_and_watchlist(self):
        from paper_trader.network import SECTORS
        for table in (self.tab(dashboard.MyAccountTab).holdings,
                      self.tab(dashboard.AiAccountTab).holdings,
                      self.tab(tabs.WatchlistTab).table):
            table.master.master.refresh() if hasattr(table.master.master, "refresh") else None
            for value, _ in self.tags_by_value(table, "Sector"):
                self.assertIn(value, SECTORS)

    def test_market_links_tab(self):
        tab = self.tab(tabs.LinksTab)
        tab.refresh()
        self.assertEqual(len(tab.links.tree.get_children()), 30)
        tab.links.tree.selection_set("1")
        self.pump()
        self.assertIn("Oil price", tab.detail.get("1.0", "end"))
        ai = load_state(self.ctx.db.conn(), "AI", self.ctx.settings())
        my = load_state(self.ctx.db.conn(), "MY", self.ctx.settings())
        expected = {f"AI:{t}" for t in ai.positions} | {f"MY:{t}" for t in
                                                       set(my.positions) | set(my.imported)}
        self.assertEqual(set(tab.owned.tree.get_children()), expected)

    def test_gain_by_ticker_page(self):
        tab = self.tab(tabs.TradeLogTab)
        tab.refresh()
        self.assertEqual([tab.pages.tab(i, "text") for i in range(2)], ["All trades", "Gain by ticker"])
        my = tab.gain_tables["MY"].tree
        self.assertTrue(my.get_children(), "demo has My account trades")
        self.check_sign_colours(self.tags_by_value(tab.gain_tables["MY"], "Total"))
        self.check_sign_colours(self.tags_by_value(tab.gain_tables["AI"], "Total"))
        self.assertIn("All together", tab.gain_notes["MY"].cget("text"))

    def test_every_setting_has_a_help_note_that_opens_and_closes(self):
        for _, rows in admin.TEXT_FIELDS:
            for key, _, _ in rows:
                self.assertTrue(admin.HELP.get(key), key)
        tab = self.tab(admin.SettingsTab)
        icons = [w for w in self.all_widgets(tab) if isinstance(w, ttk.Label)
                 and str(w.cget("style")) == "Help.TLabel"]
        self.assertGreaterEqual(len(icons), 25)
        tip = common.HelpTip(tab, "Test note")
        tip.grid(row=9, column=0)
        tip.show()
        self.pump()
        self.assertTrue(tip.popup.winfo_exists())
        tip.hide()
        self.assertIsNone(tip.popup)
        tip.toggle()  # a click opens at once ...
        self.assertIsNotNone(tip.popup)
        tip.toggle()  # ... and a second click closes
        self.assertIsNone(tip.popup)
        tip.schedule()  # hovering waits before opening; leaving cancels
        tip.hide()
        self.assertIsNone(tip.pending)

    def test_press_and_hold_shows_the_whole_row(self):
        tab = self.tab(tabs.TradeLogTab)
        tab.refresh()
        table = tab.table
        row = table.tree.get_children()[0]
        reason = table.tree.item(row, "values")[-1]
        table.open_peek(row, 100, 100)
        self.pump()
        self.assertIsNotNone(table.peek)
        self.assertIn(f"Reason:  {reason}", table.row_text(row))
        for line in table.row_text(row).splitlines():  # empty cells are left out
            self.assertTrue(line.split(":", 1)[1].strip(), line)
        table.close_peek()
        self.assertIsNone(table.peek)
        # A quick click (released before the wait) never opens it.
        class Event:
            x, y, x_root, y_root = 5, 5, 100, 100
        table.tree.see(row)
        self.pump()
        bbox = table.tree.bbox(row)
        Event.y = bbox[1] + 2
        table.on_press(Event)
        self.assertIsNotNone(table.pending)
        table.close_peek()
        self.assertIsNone(table.pending)
        self.assertIsNone(table.peek)

    def test_row_colours_follow_the_theme(self):
        table = self.tab(tabs.TradeLogTab).table
        for dark, palette in ((False, common.LIGHT), (True, common.DARK)):
            self.app.dark.set(dark)
            self.app.toggle_dark()
            for tag, key in common.ROW_TAGS.items():
                self.assertEqual(str(table.tree.tag_configure(tag, "background")), palette[key])

    def test_lessons_tab_shows_finished_trades_and_detail(self):
        tab = self.tab(lessons_ui.LessonsTab)
        tab.refresh()
        rows = tab.trades.tree.get_children()
        self.assertTrue(rows)
        self.assertIn("finished trade", tab.summary.cget("text"))
        tab.trades.tree.selection_set(rows[0])
        self.pump()
        tab.show()
        self.assertIn("Why it sold", tab.detail.get("1.0", "end"))

    def test_paper_label_is_visible(self):
        self.assertIn("PAPER MODE", self.app.title())
        self.assertIn("FAKE", self.app.title())
        self.app.refresh_status()
        self.assertIn("PAPER MODE", self.app.status.cget("text"))

    def test_manual_trade_flow_through_the_window(self):
        my = self.tab(dashboard.MyAccountTab)
        before = load_state(self.ctx.db.conn(), "MY", self.ctx.settings()).trade_count
        my.ticker.set("DEMA.TO")
        my.side_var.set("BUY")
        my.quantity.set("1")
        my.reason.set("UI test")
        my.client_id = "ui-test-1"
        self.ctx.engine  # same engine the window uses
        from paper_trader.service import refresh_one
        refresh_one(self.ctx, "DEMA.TO")
        with mock.patch.object(dashboard, "show_info") as info, \
                mock.patch.object(dashboard, "show_error") as error:
            my.show_preview()
            self.assertIn("Simulated fill", my.preview_text.get("1.0", "end"))
            my.record()
            error.assert_not_called()
            info.assert_called_once()
        after = load_state(self.ctx.db.conn(), "MY", self.ctx.settings()).trade_count
        self.assertEqual(after, before + 1)

    def test_rejected_trade_shows_a_clear_message(self):
        my = self.tab(dashboard.MyAccountTab)
        my.ticker.set("DEMA.TO")
        my.side_var.set("BUY")
        my.quantity.set("999999")
        my.show_preview()
        self.assertIn("Not allowed", my.preview_text.get("1.0", "end"))
        self.assertEqual(str(my.record_button.cget("state")), "disabled")

    def test_ai_switch_saves(self):
        ai = self.tab(dashboard.AiAccountTab)
        ai.ai_on.set(False)
        ai.save()
        self.assertFalse(self.ctx.settings().ai_enabled)
        ai.ai_on.set(True)
        ai.save()
        self.assertTrue(self.ctx.settings().ai_enabled)

    def test_watchlist_validation_and_add_remove(self):
        tab = self.tab(tabs.WatchlistTab)
        tab.ticker.set("not a ticker!")
        with mock.patch.object(tabs, "show_error") as error:
            tab.add()
            error.assert_called_once()
        tab.ticker.set("GOOG")
        with mock.patch.object(tabs, "show_error") as error:
            tab.add()
            self.assertIn("GOOG.TO", error.call_args.args[1])
        self.assertFalse(tab.table.tree.exists("GOOG"))
        tab.ticker.set("zzz.to")
        tab.add()
        self.assertTrue(tab.table.tree.exists("ZZZ.TO"))
        tab.table.tree.selection_set("ZZZ.TO")
        tab.remove()
        self.assertFalse(tab.table.tree.exists("ZZZ.TO"))

    def test_trade_log_filters(self):
        tab = self.tab(tabs.TradeLogTab)
        tab.account.set("AI account")
        tab.side.set("BUY")
        tab.refresh()
        values = [tab.table.tree.item(i, "values") for i in tab.table.tree.get_children()]
        self.assertTrue(values)
        self.assertTrue(all(v[1] == "AI account" and v[3] == "BUY" for v in values))
        tab.date_from.set("bad")
        with mock.patch.object(tabs, "show_error") as error:
            tab.apply()
            error.assert_called_once()
        tab.date_from.set("")
        tab.account.set("All")
        tab.side.set("All")

    def test_settings_rejects_bad_input_and_saves_good(self):
        tab = self.tab(admin.SettingsTab)
        tab.refresh()
        tab.vars["max_trade_size"].set("oops")
        with mock.patch.object(admin, "show_error") as error:
            tab.save()
            error.assert_called_once()
        tab.vars["max_trade_size"].set("750")
        tab.vars["max_daily_buys"].set("4")
        with mock.patch.object(admin, "show_info"):
            tab.save()
        s = self.ctx.settings()
        self.assertEqual((D(s.max_trade_size), s.max_daily_buys), (D("750"), 4))

    def test_reset_requires_typed_confirmation(self):
        tab = self.tab(admin.AboutTab)
        count = self.ctx.db.conn().execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        with mock.patch.object(admin.messagebox, "askyesnocancel", return_value=False), \
                mock.patch.object(admin.simpledialog, "askstring", return_value="reset"), \
                mock.patch.object(admin, "show_info") as info:
            tab.reset()
            info.assert_called_once_with(tab, "Nothing was changed.")
        self.assertEqual(self.ctx.db.conn().execute("SELECT COUNT(*) FROM trades").fetchone()[0], count)


if __name__ == "__main__":
    unittest.main()
