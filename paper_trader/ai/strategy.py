"""The default AI-account strategy: transparent rules, not a prediction engine.

Price data decides whether a trade is even considered. News can raise confidence or
veto a buy, but no news item can start a trade by itself. Nothing here claims an edge;
finding out how this does against you is the point of the experiment.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from ..ledger import Position
from ..money import D
from ..news.signals import Signal

MIN_BARS = 50
NAME = "momentum+news rules v1"


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


@dataclass
class Proposal:
    action: str  # BUY, SELL or HOLD
    explanation: str
    confidence: float | None = None
    metrics: dict = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)


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
        "sma50": round(_sma(closes, 50), 4),
        "momentum_5d_pct": round((price / closes[-5] - 1) * 100, 2),
        "momentum_20d_pct": round((price / closes[-20] - 1) * 100, 2),
    }
    if len(volumes) >= 21 and _sma(volumes[:-1], 20) > 0:
        metrics["volume_vs_20d"] = round(volumes[-1] / _sma(volumes[:-1], 20), 2)
    return metrics


class MomentumNewsStrategy(Strategy):
    name = NAME

    def evaluate(self, data: TickerInput) -> Proposal:
        pos = data.position
        if pos is not None:
            floor = pos.avg_cost * (1 - data.stop_loss_pct / 100)
            if data.price <= floor:
                return Proposal(
                    "SELL", f"Stop-loss: price {data.price} {data.currency} is at or below "
                    f"{floor:.4f}, which is {data.stop_loss_pct}% under the average cost.", 0.95,
                    {"stop_floor": str(floor)})
        if len(data.closes) < MIN_BARS:
            return Proposal("HOLD", f"Fewer than {MIN_BARS} days of price history.")

        price = float(data.price)
        m = compute_metrics(price, data.closes, data.volumes)
        negative = [x for x in data.signals if x.direction < 0]
        positive = [x for x in data.signals if x.direction > 0]

        if pos is not None:
            if price < m["sma20"] and m["momentum_5d_pct"] < -2:
                confidence = 0.65 if negative else 0.6
                note = f" {len(negative)} negative unverified signal(s) noted." if negative else ""
                return Proposal(
                    "SELL", f"Trend exit: price is below its 20-day average and down "
                    f"{abs(m['momentum_5d_pct'])}% over 5 days.{note}", confidence, m, negative)
            return Proposal("HOLD", "Holding: no exit condition met.", None, m)

        uptrend = (price > m["sma20"] > m["sma50"] and m["momentum_20d_pct"] > 0
                   and m["momentum_5d_pct"] > 0)
        if not uptrend:
            return Proposal("HOLD", "No entry: price trend conditions not met.", None, m)
        if negative:
            return Proposal("HOLD", "Entry vetoed: recent negative unverified signal.", None, m,
                            negative)
        confidence = 0.55
        reasons = [f"Price is above its 20-day and 50-day averages, up "
                   f"{m['momentum_20d_pct']}% over 20 days"]
        if m.get("volume_vs_20d", 0) > 1.2:
            confidence += 0.05
            reasons.append(f"latest volume is {m['volume_vs_20d']}x its 20-day average")
        if positive:
            confidence += min(0.05 * len(positive), 0.15)
            reasons.append(f"{len(positive)} supporting unverified news signal(s)")
        return Proposal("BUY", "; ".join(reasons) + ".", round(confidence, 2), m, positive)


def to_float(values) -> list[float]:
    return [float(D(v)) for v in values]
