"""The two account tabs."""
from __future__ import annotations

import threading
import tkinter as tk
import uuid
from tkinter import messagebox, ttk

from ..engine import NeedsConfirmation, TradePlan, TradeRejected, TradeRequest
from ..money import D, fmt_money, fmt_pct, fmt_price, fmt_qty, fmt_signed
from ..performance import load_snapshots, period_returns
from ..service import refresh_one
from ..settings import save_settings
from ..network import sector_for, sector_map
from ..symbols import normalize_ticker
from ..timeutil import local_str, to_local
from ..valuation import account_view, budget, imported_views
from .common import (Table, Tile, bold_num, colors, form_row, show_error, show_info,
                     sign_tag, text_box)

HOLDING_COLUMNS = [("Ticker", 90), ("Sector", 150), ("Qty", 60), ("Ccy", 45), ("Avg cost", 85),
                   ("Ref price", 85), ("Price time", 125), ("Data", 130), ("Value CAD", 100),
                   ("Unrealized CAD", 110)]


class AccountTab(ttk.Frame):
    account = "MY"
    title = ""

    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.ctx = app.ctx
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        ttk.Label(self, text=f"{self.title}  (PAPER, simulated)", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        tiles = ttk.Frame(self)
        tiles.grid(row=1, column=0, sticky="we")
        names = (("cash", "Cash available"), ("value", "Holdings value"), ("total", "Total account value"),
                 ("ret", "Total return"), ("real", "Realized gain/loss"), ("unreal", "Unrealized gain/loss"))
        self.tiles = {}
        for i, (key, label) in enumerate(names):
            self.tiles[key] = Tile(tiles, label)
            self.tiles[key].grid(row=i // 3, column=i % 3, sticky="w")

        self.limit_label = ttk.Label(self)
        self.limit_label.grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.perf_label = ttk.Label(self)
        self.perf_label.grid(row=3, column=0, sticky="nw", pady=(2, 6))

        body = ttk.Frame(self)
        body.grid(row=4, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self.warning = ttk.Label(body, style="Warn.TLabel")
        self.warning.grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="Green rows: in profit.  Red rows: at a loss.",
                  style="Muted.TLabel").grid(row=0, column=0, sticky="e")
        self.holdings = Table(body, HOLDING_COLUMNS, height=6)
        self.holdings.grid(row=1, column=0, sticky="nsew")
        self.build_extra(body)

        side = ttk.Frame(self)
        side.grid(row=1, column=1, rowspan=4, sticky="ns", padx=(12, 0))
        self.build_side(side)

    def build_extra(self, body) -> None:
        pass

    def build_side(self, side) -> None:
        pass

    def refresh(self) -> None:
        conn = self.ctx.db.conn()
        s = self.ctx.settings()
        now = self.ctx.clock()
        view = account_view(conn, self.account, s, now)
        b = budget(conn, self.account, s, now)
        self.tiles["cash"].set(fmt_money(view.cash))
        self.tiles["value"].set(fmt_money(view.market_value))
        self.tiles["total"].set(fmt_money(view.total_value))
        self.tiles["ret"].set(fmt_pct(view.total_return_pct))
        self.tiles["real"].set(fmt_signed(view.realized))
        self.tiles["unreal"].set(fmt_signed(view.unrealized))
        self.limit_label.configure(text=(
            f"Can buy now: {fmt_money(b.buying_power)}   |   {b.period.capitalize()} limit: "
            f"{fmt_money(b.used)} used of {fmt_money(b.limit)}   |   Allowance resets "
            f"{to_local(b.resets_at).strftime('%a %Y-%m-%d %H:%M')} (no cash is added)"))
        returns = period_returns(load_snapshots(conn, self.account), view.total_value, now)
        self.perf_label.configure(text="Performance:  " + "   ".join(
            f"{r.label} {fmt_pct(r.pct) if r.pct is not None else 'n/a'}" for r in returns))
        self.warning.configure(text=(
            f"Partial valuation, no current price for: {', '.join(view.missing)}"
            if view.missing else ""))
        sectors = sector_map(conn)
        self.holdings.set_rows([(h.ticker, (
            h.ticker, sector_for(h.ticker, sectors), fmt_qty(h.quantity), h.currency,
            fmt_price(h.avg_cost), bold_num(fmt_price(h.price)), local_str(h.quote_ts), h.status,
            f"{h.market_value:,.2f}" if h.market_value is not None else "n/a",
            f"{h.unrealized:+,.2f}" if h.unrealized is not None else "n/a"),
            sign_tag(h.unrealized)) for h in view.holdings])
        return view


class MyAccountTab(AccountTab):
    account = "MY"
    title = "My account"

    def build_extra(self, body) -> None:
        ttk.Label(body, text="Imported holdings: tracked separately, excluded from account value, "
                  "returns, cash and the AI comparison", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        self.imported = Table(body, [("Ticker", 90), ("Sector", 150), ("Qty", 60), ("Ccy", 45), ("Avg cost", 85),
                                     ("Ref price", 85), ("Value (own ccy)", 110),
                                     ("Unrealized (own ccy)", 130), ("Data", 130)], height=3)
        self.imported.grid(row=3, column=0, sticky="nsew")
        self.imported_note = ttk.Label(body, style="Muted.TLabel")
        self.imported_note.grid(row=4, column=0, sticky="w")

    def build_side(self, side) -> None:
        ticket = ttk.LabelFrame(side, text="Record a paper trade", padding=8)
        ticket.pack(fill="x")
        ticket.columnconfigure(1, weight=1)
        self.ticker = tk.StringVar()
        self.side_var = tk.StringVar(value="BUY")
        self.quantity = tk.StringVar()
        self.reason = tk.StringVar()
        self.sleeve = tk.StringVar(value="TRADING")
        self.ticker_box = ttk.Combobox(ticket, textvariable=self.ticker, width=22,
                                       postcommand=self.mark_owned)
        self.ticker_box.bind("<<ComboboxSelected>>", self.pick_ticker)
        self.owned: dict[str, str] = {}
        form_row(ticket, 0, "Ticker", self.ticker_box)
        sides = ttk.Frame(ticket)
        for text in ("BUY", "SELL"):
            ttk.Radiobutton(sides, text=text, value=text, variable=self.side_var).pack(side="left")
        form_row(ticket, 1, "Action", sides)
        form_row(ticket, 2, "Quantity", ttk.Entry(ticket, textvariable=self.quantity))
        form_row(ticket, 3, "Reason", ttk.Entry(ticket, textvariable=self.reason))
        ttk.Checkbutton(ticket, text="Sell from imported holdings", variable=self.sleeve,
                        onvalue="IMPORTED", offvalue="TRADING").grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.preview_button = ttk.Button(ticket, text="Get price and preview", command=self.preview)
        self.preview_button.grid(row=5, column=0, columnspan=2, sticky="we", pady=(6, 2))
        self.preview_text = text_box(ticket, height=8, width=36)
        self.preview_text.grid(row=6, column=0, columnspan=2, sticky="we", pady=2)
        self.record_button = ttk.Button(ticket, text="Record paper trade", command=self.record,
                                        state="disabled")
        self.record_button.grid(row=7, column=0, columnspan=2, sticky="we")
        self.client_id = ""
        for var in (self.ticker, self.side_var, self.quantity, self.sleeve):
            var.trace_add("write", lambda *_: self.clear_preview())

        form = ttk.LabelFrame(side, text="Add an existing holding", padding=8)
        form.pack(fill="x", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        self.imp_ticker, self.imp_qty, self.imp_price = tk.StringVar(), tk.StringVar(), tk.StringVar()
        form_row(form, 0, "Ticker", ttk.Entry(form, textvariable=self.imp_ticker, width=18))
        form_row(form, 1, "Quantity", ttk.Entry(form, textvariable=self.imp_qty))
        form_row(form, 2, "Avg price", ttk.Entry(form, textvariable=self.imp_price))
        ttk.Label(form, text="Own currency. Uses no cash and creates no buying power.",
                  style="Muted.TLabel", wraplength=250).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(form, text="Add imported holding", command=self.add_import).grid(
            row=4, column=0, columnspan=2, sticky="we", pady=(6, 0))

    def refresh(self) -> None:
        view = super().refresh()
        conn = self.ctx.db.conn()
        s = self.ctx.settings()
        rows = imported_views(conn, view.state, s, self.ctx.clock())
        sectors = sector_map(conn)
        self.imported.set_rows([(h.ticker, (
            h.ticker, sector_for(h.ticker, sectors), fmt_qty(h.quantity), h.currency, fmt_price(h.avg_cost), bold_num(fmt_price(h.price)),
            f"{h.market_value:,.2f}" if h.market_value is not None else "n/a",
            f"{h.unrealized:+,.2f}" if h.unrealized is not None else "n/a", h.status),
            sign_tag(h.unrealized)) for h in rows])
        realized = ", ".join(fmt_signed(v, c) for c, v in view.state.imported_realized.items())
        proceeds = ", ".join(fmt_money(v, c) for c, v in view.state.imported_proceeds.items())
        self.imported_note.configure(
            text=f"Imported sales: realized {realized}; proceeds kept outside trading cash: {proceeds}"
            if realized else "")
        tickers = {r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist")}
        self.owned = owned_amounts(view.state)
        self.ticker_box["values"] = [ticker_choice(t, self.owned.get(t))
                                     for t in sorted(tickers | set(self.owned))]

    # -- ticker dropdown ------------------------------------------------------

    def chosen_ticker(self) -> str:
        """The ticker typed or picked, without the amount shown in the list."""
        text = self.ticker.get().strip()
        return text.split()[0] if text else text

    def pick_ticker(self, _event=None) -> None:
        self.ticker.set(self.chosen_ticker())
        self.ticker_box.selection_clear()

    def listbox(self) -> str:
        popdown = self.ticker_box.tk.eval(f"ttk::combobox::PopdownWindow {self.ticker_box}")
        return f"{popdown}.f.l"

    def mark_owned(self) -> None:
        """Monospaced list so the amounts line up, and owned tickers in yellow."""
        box = self.listbox()
        tcl = self.ticker_box.tk
        tcl.call(box, "configure", "-font", ("Consolas", 10))

        def paint():  # Tk fills the list after this callback returns
            if not tcl.call("winfo", "exists", box):
                return
            c = colors()
            for i, value in enumerate(self.ticker_box["values"]):
                if value.split()[0] in self.owned:
                    tcl.call(box, "itemconfigure", i, "-background", c["held_bg"],
                             "-foreground", c["ink"])
        self.after_idle(paint)

    # -- trade ticket ---------------------------------------------------------

    def request(self, **flags) -> TradeRequest:
        return TradeRequest("MY", "MANUAL", self.side_var.get(), self.chosen_ticker(),
                            self.quantity.get(), self.reason.get() or "(preview)",
                            client_id=self.client_id, sleeve=self.sleeve.get(), **flags)

    def clear_preview(self) -> None:
        self.record_button.configure(state="disabled")
        self.write_preview("")

    def write_preview(self, text: str) -> None:
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")

    def preview(self) -> None:
        try:
            ticker = normalize_ticker(self.chosen_ticker())
        except ValueError as exc:
            show_error(self, str(exc))
            return
        self.preview_button.configure(state="disabled", text="Fetching price...")

        def work():
            try:
                refresh_one(self.ctx, ticker)
            finally:
                self.ctx.db.close()
                self.app.post(self.show_preview)

        threading.Thread(target=work, daemon=True).start()

    def show_preview(self) -> None:
        self.preview_button.configure(state="normal", text="Get price and preview")
        self.client_id = f"manual:{uuid.uuid4()}"
        try:
            plan = self.ctx.engine.preview(self.request(confirm_stale=True))
        except TradeRejected as exc:
            self.write_preview(f"Not allowed:\n{exc}")
            self.record_button.configure(state="disabled")
            return
        self.write_preview(describe(plan))
        self.record_button.configure(state="normal")

    def record(self) -> None:
        if not self.reason.get().strip():
            show_error(self, "Enter a reason for this trade.")
            return
        flags = {}
        while True:
            try:
                request = self.request(**flags)
                request.reason = self.reason.get()
                self.ctx.engine.submit(request)
                break
            except NeedsConfirmation as exc:
                if not messagebox.askyesno("Please confirm", str(exc), parent=self):
                    return
                flags["confirm_stale" if exc.code == "STALE_QUOTE" else "confirm_duplicate"] = True
            except TradeRejected as exc:
                show_error(self, str(exc))
                return
        self.quantity.set("")
        self.reason.set("")
        self.app.refresh_all()
        show_info(self, "Paper trade recorded. No real order was placed.")

    def add_import(self) -> None:
        try:
            self.ctx.engine.import_holding(self.imp_ticker.get(), self.imp_qty.get(),
                                           self.imp_price.get())
        except (TradeRejected, ValueError) as exc:
            show_error(self, str(exc))
            return
        for var in (self.imp_ticker, self.imp_qty, self.imp_price):
            var.set("")
        self.app.worker.request_refresh()
        self.app.refresh_all()


CHOICE_WIDTH = 22


def owned_amounts(state) -> dict[str, str]:
    """Ticker -> amount owned in My account, e.g. '12' or '12 +5 imp'."""
    out = {}
    for t in set(state.positions) | set(state.imported):
        parts = []
        if t in state.positions:
            parts.append(fmt_qty(state.positions[t].quantity))
        if t in state.imported:
            parts.append(("+" if parts else "") + f"{fmt_qty(state.imported[t].quantity)} imp")
        out[t] = " ".join(parts)
    return out


def ticker_choice(ticker: str, amount: str | None) -> str:
    """A dropdown line: the ticker on the left, the amount owned on the right."""
    if not amount:
        return ticker
    gap = max(2, CHOICE_WIDTH - len(ticker) - len(amount))
    return f"{ticker}{' ' * gap}{amount}"


def describe(plan: TradePlan) -> str:
    c = plan.currency
    lines = [
        f"{plan.request.side} {fmt_qty(plan.quantity)} {plan.ticker}",
        f"Reference price: {fmt_price(plan.reference_price, c)}",
        f"Simulated fill: {fmt_price(plan.fill_price, c)} (adjustment "
        f"{fmt_price(plan.slippage)}, rule {plan.slippage_rule})",
        f"Price time: {local_str(plan.quote_ts)}, source {plan.quote_source}",
    ]
    if plan.fx_rate is not None:
        lines.append(f"USD/CAD {plan.fx_rate} from {plan.fx_date} ({plan.fx_source})")
    if plan.request.sleeve == "IMPORTED":
        lines.append(f"Proceeds {fmt_money(plan.gross_native, c)} stay outside trading cash")
    else:
        lines.append(f"Cash change: {fmt_signed(plan.cash_delta)}; after: {fmt_money(plan.cash_after)}")
    if plan.end_of_day:
        lines.append("This is the last closing price, not a live price.")
    if plan.stale:
        lines.append("WARNING: this quote is stale.")
    return "\n".join(lines)


PLAN_COLUMNS = [("Ticker", 90), ("Plan", 150), ("Sell target", 85), ("Stop", 105),
                ("Buy more at", 90), ("Ref price", 85), ("Plan time", 125), ("Why", 340)]


def plan_rows(rows, held) -> list[tuple]:
    """Table rows for the AI plans of tickers still held. Sell-soon rows are red."""
    return [(r["ticker"], (
        r["ticker"], r["plan"], r["target"],
        r["stop"] + (" (raised)" if r["trailing"] else ""),
        r["add_at"] or "not now",
        bold_num(fmt_price(r["price"])) if r["price"] else "n/a",
        local_str(r["ts"]), r["why"]), "loss" if r["plan"].startswith("Sell soon") else "")
        for r in rows if r["ticker"] in held]


class AiAccountTab(AccountTab):
    account = "AI"
    title = "AI account"

    def build_extra(self, body) -> None:
        ttk.Label(body, text="AI plan for each holding: hold or sell soon (rough trading days), "
                  "sell target, stop (it rises with the price) and where it would buy more. "
                  "Rule-based estimates, not forecasts. Red rows: sell soon. Hold a row to read it all.",
                  style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.plans = Table(body, PLAN_COLUMNS, height=4)
        self.plans.grid(row=3, column=0, sticky="nsew")

    def build_side(self, side) -> None:
        box = ttk.LabelFrame(side, text="AI trading switch", padding=8)
        box.pack(fill="x")
        self.ai_on = tk.BooleanVar()
        self.suggest = tk.BooleanVar()
        ttk.Checkbutton(box, text="AI trading ON", variable=self.ai_on, command=self.save).pack(anchor="w")
        ttk.Checkbutton(box, text="Suggest only (log decisions, make no trades)",
                        variable=self.suggest, command=self.save).pack(anchor="w")
        self.state_label = ttk.Label(box, wraplength=260)
        self.state_label.pack(anchor="w", pady=(6, 0))
        limits = ttk.LabelFrame(side, text="Limits in force", padding=8)
        limits.pack(fill="x", pady=(10, 0))
        self.limits_label = ttk.Label(limits, justify="left")
        self.limits_label.pack(anchor="w")
        ttk.Label(limits, text="Change these in Settings.", style="Muted.TLabel").pack(anchor="w")
        note = ttk.LabelFrame(side, text="What the AI can do", padding=8)
        note.pack(fill="x", pady=(10, 0))
        ttk.Label(note, wraplength=260, justify="left", text=(
            "It records simulated trades in this account only. It cannot change My account, and "
            "the app has no way to place real orders. There is no claim that it will be "
            "profitable.")).pack(anchor="w")

    def save(self) -> None:
        s = self.ctx.settings()
        s.ai_enabled = self.ai_on.get()
        s.ai_suggest_only = self.suggest.get()
        save_settings(self.ctx.db, s, self.ctx.clock)
        self.app.refresh_all()

    def refresh(self) -> None:
        view = super().refresh()
        rows = self.ctx.db.conn().execute("SELECT * FROM ai_plans ORDER BY ticker").fetchall()
        self.plans.set_rows(plan_rows(rows, view.state.positions))
        s = self.ctx.settings()
        self.ai_on.set(s.ai_enabled)
        self.suggest.set(s.ai_suggest_only)
        state = "OFF: the AI makes no decisions." if not s.ai_enabled else (
            "ON, suggest only." if s.ai_suggest_only else "ON: simulated trades run after each "
            f"price update, every {s.refresh_minutes} min, while the app is open.")
        self.state_label.configure(text=state)
        review = "on" if s.llm_review_enabled else "off"
        self.limits_label.configure(text=(
            f"Max trade size: {fmt_money(s.max_trade_size)}\n"
            f"Max buys per day: {s.max_daily_buys}\n"
            f"Max positions: {s.max_positions}\n"
            f"Max exposure: {D(s.max_exposure_pct)}% of account\n"
            f"Stop: {D(s.stop_loss_pct)}% below cost, raised as the\n"
            f"  price makes new highs\n"
            f"Min confidence: {s.min_confidence}\n"
            f"Strategy: {self.ctx.strategy.name}\n"
            f"LLM review: {review}"))
