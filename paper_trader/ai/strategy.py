"""The default AI-account strategy: transparent rules, not a prediction engine.

Price data decides whether a trade is even considered. News is counted as good against bad
votes (sector news at half weight): the bigger side wins and a tie is neutral. Positive news
raises confidence, Negative news blocks a buy, and no news item can start a trade by itself. Nothing here claims an edge;
finding out how this does against you is the point of the experiment.

Every holding also gets a plan: hold or sell soon (with a rough number of trading days),
a sell target, a stop that rises with the price, and a price to buy more at. The plan
numbers come from the same fixed rules; they are not forecasts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from decimal import Decimal

from ..ledger import Position
from ..money import D
from ..news.signals import Signal, tally, voting

MIN_BARS = 50
NAME = "momentum+news rules v3"
REWARD_RISK = Decimal(2)          # the target sits twice as far above cost as the stop sits below
MAX_ADDS = 1                      # buy more at most once per holding
ADD_BAND = Decimal("1.01")        # "buy more" zone: up to 1% above the 20-day average
REENTRY_DAYS = 3                  # no new buy of a ticker within this many days of selling it
SELL_SOON_GAP = Decimal("0.02")   # within 2% of the stop counts as "sell soon"
MAX_DAYS = 60                     # longer time estimates are shown as "N+ days"
CENTS = Decimal("0.01")
NEWS_STEP = 0.05                  # confidence added per vote of lead when news is Positive ...
NEWS_MAX = 0.15                   # ... up to this much


@dataclass(frozen=True)
class TickerInput:
    ticker: str
    price: Decimal
    currency: str
    closes: list[float]    # completed daily closes, oldest first
    volumes: list[float]
    position: Position | None
    signals: list[Signal]
    stop_loss_pct: Decimal
    peak: Decimal | None = None           # highest price seen since the holding was opened
    buys_since_open: int = 0              # AI buys of this ticker since the holding was opened
    days_since_sell: float | None = None  # days since the AI last sold this ticker


def _s(value) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class Plan:
    """What the AI intends to do with a holding, recalculated at every price update."""
    stance: str               # "Hold" or "Sell soon"
    days_low: int | None      # rough range in trading days; None when there is no estimate
    days_high: int | None     # None with a days_low means "days_low or more"
    target: Decimal           # take-profit price
    stop: Decimal             # sell if the price falls to this
    trailing: bool            # True when a higher price has raised the stop
    add_at: Decimal | None    # buy more at or below this, only while the uptrend holds
    why: str
    news: str = ""            # the news score when the plan was made, e.g. "Positive (6/4, +2)"

    def label(self) -> str:
        if self.days_low is None:
            return f"{self.stance} (no time estimate)"
        if self.days_high is None:
            return f"{self.stance} ({self.days_low}+ days)"
        return f"{self.stance} ({self.days_low}-{self.days_high} days)"

    def as_dict(self) -> dict:
        return {"plan": self.label(), "target": str(self.target), "stop": str(self.stop),
                "trailing_stop": self.trailing, "add_at": _s(self.add_at), "why": self.why,
                "news": self.news}

    def short(self) -> str:
        parts = [self.label(), f"target {self.target}", f"stop {self.stop}"]
        if self.add_at is not None:
            parts.append(f"buy more at {self.add_at}")
        return ", ".join(parts)


@dataclass
class Proposal:
    action: str  # BUY, SELL or HOLD
    explanation: str
    confidence: float | None = None
    metrics: dict = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    plan: Plan | None = None


class Strategy(ABC):
    name = "base"

    @abstractmethod
    def evaluate(self, data: TickerInput) -> Proposal:
        """Decide for one ticker. Sizing and all rule checks happen elsewhere."""


def _sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def compute_metrics(price: float, closes: list[float], volumes: list[float]) -> dict:
    metrics = {
        "sma20": round(_sma(closes, 20), 4),
        "sma20_5d_ago": round(_sma(closes[:-5], 20), 4),
        "sma50": round(_sma(closes, 50), 4),
        "momentum_5d_pct": round((price / closes[-5] - 1) * 100, 2),
        "momentum_20d_pct": round((price / closes[-20] - 1) * 100, 2),
    }
    if len(volumes) >= 21 and _sma(volumes[:-1], 20) > 0:
        metrics["volume_vs_20d"] = round(volumes[-1] / _sma(volumes[:-1], 20), 2)
    return metrics


def is_uptrend(price: float, m: dict) -> bool:
    return (price > m["sma20"] > m["sma50"] and m["momentum_20d_pct"] > 0
            and m["momentum_5d_pct"] > 0)


def trend_intact(price: float, m: dict) -> bool:
    """Looser than is_uptrend: allows a short dip, which is when buying more makes sense."""
    return price > m["sma20"] > m["sma50"] and m["momentum_20d_pct"] > 0


def stop_price(cost: Decimal, peak: Decimal | None, pct: Decimal) -> tuple[Decimal, bool]:
    """The higher of: pct below cost, and pct below the highest price since buying."""
    keep = 1 - pct / 100
    fixed = (cost * keep).quantize(CENTS)
    if peak is not None and peak > cost:
        trailing = (peak * keep).quantize(CENTS)
        if trailing > fixed:
            return trailing, True
    return fixed, False


def make_plan(price: Decimal, cost: Decimal, peak: Decimal | None, pct: Decimal,
              closes: list[float], m: dict | None, adds_left: bool) -> Plan:
    """Plan for a holding bought at `cost` (or about to be bought at `price`)."""
    stop, trailing = stop_price(cost, peak, pct)
    target = (cost * (1 + REWARD_RISK * pct / 100)).quantize(CENTS)
    if m is None:
        return Plan("Hold", None, None, target, stop, trailing, None,
                    "Not enough price history for a trend reading; only the stop and target apply.")
    p = float(price)
    if p < m["sma20"] or price - stop <= price * SELL_SOON_GAP:
        reason = ("the price is below its 20-day average" if p < m["sma20"]
                  else f"the price is within {SELL_SOON_GAP * 100:.0f}% of the stop")
        return Plan("Sell soon", 1, 3, target, stop, trailing, None,
                    f"Weak: {reason}. A sell is likely at the stop or on a trend exit.")
    add_at = None
    if adds_left and trend_intact(p, m):
        zone = (Decimal(str(m["sma20"])) * ADD_BAND).quantize(CENTS)
        if zone > stop and zone >= cost:
            add_at = zone
    drift = (p - closes[-20]) / 20  # average gain per day over the last 20 days
    if price >= target:
        return Plan("Hold", None, None, target, stop, trailing, add_at,
                    "The target is already reached.")
    if drift <= 0:
        return Plan("Hold", None, None, target, stop, trailing, add_at,
                    "Trend intact, but no upward pace over 20 days to time the target.")
    est = float(target - price) / drift
    low = min(max(2, round(est * 0.7)), MAX_DAYS)
    high = round(est * 1.5) if est * 1.5 <= MAX_DAYS else None
    return Plan("Hold", low, high, target, stop, trailing, add_at,
                f"Trend intact; target about {est:.0f} trading days away at the 20-day pace.")


class MomentumNewsStrategy(Strategy):
    name = NAME

    def evaluate(self, data: TickerInput) -> Proposal:
        if data.position is not None:
            return self._held(data, data.position)
        return self._entry(data)

    def _held(self, data: TickerInput, pos: Position) -> Proposal:
        pct = data.stop_loss_pct
        stop, trailing = stop_price(pos.avg_cost, data.peak, pct)
        if data.price <= stop:
            if trailing:
                why = (f"Trailing stop: price {data.price} {data.currency} is at or below {stop}, "
                       f"which is {pct}% under the highest price since buying ({data.peak}).")
            else:
                why = (f"Stop-loss: price {data.price} {data.currency} is at or below {stop}, "
                       f"which is {pct}% under the average cost.")
            return Proposal("SELL", why, 0.95, {"stop": str(stop), "peak": _s(data.peak)})
        if len(data.closes) < MIN_BARS:
            plan = make_plan(data.price, pos.avg_cost, data.peak, pct, data.closes, None, False)
            return Proposal("HOLD", f"Fewer than {MIN_BARS} days of price history.", plan=plan)

        price = float(data.price)
        m = compute_metrics(price, data.closes, data.volumes)
        news = tally(data.signals)
        votes = voting(data.signals)
        bad_news = news.status == "Negative"
        plan = replace(make_plan(data.price, pos.avg_cost, data.peak, pct, data.closes, m,
                                 data.buys_since_open <= MAX_ADDS), news=news.label())

        if data.price >= plan.target:
            return Proposal("SELL", f"Target reached: price {data.price} is at or above the sell "
                            f"target {plan.target}.", 0.8, m, plan=plan)
        below = price < m["sma20"]
        fast_drop = below and m["momentum_5d_pct"] < -2
        slow_slide = below and m["sma20"] < m["sma20_5d_ago"] and m["momentum_20d_pct"] < 0
        if fast_drop or slow_slide:
            confidence = 0.65 if bad_news else 0.6
            note = f" News {news.label()}." if bad_news else ""
            if fast_drop:
                why = (f"Trend exit: price is below its 20-day average and down "
                       f"{abs(m['momentum_5d_pct'])}% over 5 days.")
            else:
                why = (f"Trend exit (slow slide): price is below its 20-day average, the average "
                       f"itself is falling, and the price is down {abs(m['momentum_20d_pct'])}% "
                       f"over 20 days.")
            return Proposal("SELL", why + note, confidence, m, votes if bad_news else [], plan)

        if plan.add_at is not None and pos.avg_cost <= data.price <= plan.add_at:
            if bad_news:
                return Proposal("HOLD", f"Buy more vetoed: news {news.label()}.",
                                None, m, votes, plan)
            return Proposal("BUY", f"Buy more: the uptrend holds and the price pulled back to "
                            f"{data.price}, within 1% of its 20-day average. News "
                            f"{news.label()}.", 0.55, m, votes, plan)
        return Proposal("HOLD", "Holding: no exit condition met.", None, m, plan=plan)

    def _entry(self, data: TickerInput) -> Proposal:
        if len(data.closes) < MIN_BARS:
            return Proposal("HOLD", f"Fewer than {MIN_BARS} days of price history.")
        price = float(data.price)
        m = compute_metrics(price, data.closes, data.volumes)
        news = tally(data.signals)
        votes = voting(data.signals)

        if not is_uptrend(price, m):
            return Proposal("HOLD", "No entry: price trend conditions not met.", None, m)
        if data.days_since_sell is not None and data.days_since_sell < REENTRY_DAYS:
            return Proposal("HOLD", f"No entry: sold less than {REENTRY_DAYS} days ago.", None, m)
        if news.status == "Negative":
            return Proposal("HOLD", f"Entry vetoed: news {news.label()}.", None, m, votes)
        confidence = 0.55
        reasons = [f"Price is above its 20-day and 50-day averages, up "
                   f"{m['momentum_20d_pct']}% over 20 days"]
        if m.get("volume_vs_20d", 0) > 1.2:
            confidence += 0.05
            reasons.append(f"latest volume is {m['volume_vs_20d']}x its 20-day average")
        if news.status == "Positive":
            confidence += min(NEWS_STEP * news.net, NEWS_MAX)
        if votes:
            reasons.append(f"news {news.label()}")
        plan = replace(make_plan(data.price, data.price, None, data.stop_loss_pct, data.closes, m,
                                 True), news=news.label())
        return Proposal("BUY", "; ".join(reasons) + ".", round(confidence, 2), m, votes, plan)


def to_float(values) -> list[float]:
    return [float(D(v)) for v in values]
