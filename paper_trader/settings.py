"""User settings, persisted in SQLite. Every change is written to settings_audit."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal

from .db import Database
from .money import D, parse_decimal
from .symbols import normalize_ticker
from .timeutil import Clock, to_iso

MARKET_PROVIDERS = ("yahoo", "alphavantage", "twelvedata")


def default_categories() -> dict:
    # Values are starting assumptions for the simulation, not measured spreads.
    # "abs" is per share in the ticker's own currency; "pct" is percent of price.
    return {
        "liquid": {"mode": "abs", "value": "0.02"},
        "normal": {"mode": "abs", "value": "0.03"},
        "illiquid": {"mode": "pct", "value": "0.25"},
    }


@dataclass
class Settings:
    dark_mode: bool = True
    # Account rules: identical for both accounts.
    starting_balance: str = "10000"
    period_limit: str = ""  # blank means "same as the starting balance"
    reset_period: str = "daily"
    allow_fractional: bool = False
    cad_only: bool = True  # block new US-listed positions, for both accounts
    # Data freshness.
    market_provider: str = "yahoo"
    max_quote_age_minutes: int = 60
    max_eod_age_days: int = 4  # end-of-day prices: covers a long weekend
    max_fx_age_days: int = 5
    refresh_minutes: int = 30  # 48 runs/day: up to 16 tickers within 800 Twelve Data credits
    snapshot_minutes: int = 60
    news_minutes: int = 60
    # Simulated spread/slippage.
    slippage_categories: dict = field(default_factory=default_categories)
    default_category: str = "normal"
    ticker_categories: dict = field(default_factory=dict)
    # AI account only.
    ai_enabled: bool = False
    ai_suggest_only: bool = False
    max_trade_size: str = "1000"
    max_daily_buys: int = 3
    max_positions: int = 5
    max_exposure_pct: str = "80"
    stop_loss_pct: str = "8"
    min_confidence: str = "0.55"
    news_lookback_hours: int = 48
    market_links_enabled: bool = True  # 1.3.0: sector links from headlines
    llm_review_enabled: bool = False
    llm_model: str = "claude-opus-5-5"
    llm_max_reviews_per_day: int = 10

    @property
    def limit(self) -> Decimal:
        return D(self.period_limit) if self.period_limit.strip() else D(self.starting_balance)

    @property
    def start(self) -> Decimal:
        return D(self.starting_balance)


def _positive(text: str, label: str, errors: list[str]) -> None:
    try:
        if parse_decimal(text, label) <= 0:
            errors.append(f"{label} must be greater than zero.")
    except ValueError as exc:
        errors.append(str(exc))


def validate(s: Settings) -> list[str]:
    errors: list[str] = []
    _positive(s.starting_balance, "Starting balance", errors)
    if s.period_limit.strip():
        _positive(s.period_limit, "Period buy limit", errors)
    if s.reset_period not in ("daily", "weekly"):
        errors.append("Limit reset must be daily or weekly.")
    if s.market_provider not in MARKET_PROVIDERS:
        errors.append("Unknown market data provider.")
    _positive(s.max_trade_size, "Maximum trade size", errors)
    for label, value in (("Maximum exposure", s.max_exposure_pct), ("Stop-loss", s.stop_loss_pct)):
        _positive(value, label, errors)
        try:
            if parse_decimal(value, label) > 100:
                errors.append(f"{label} cannot be above 100%.")
        except ValueError:
            pass
    try:
        if not 0 <= parse_decimal(s.min_confidence, "Minimum confidence") <= 1:
            errors.append("Minimum confidence must be between 0 and 1.")
    except ValueError as exc:
        errors.append(str(exc))
    for label, value, low in (
        ("Maximum quote age", s.max_quote_age_minutes, 1),
        ("Maximum end-of-day age", s.max_eod_age_days, 1),
        ("Maximum FX age", s.max_fx_age_days, 0),
        ("Refresh interval", s.refresh_minutes, 1),
        ("Snapshot interval", s.snapshot_minutes, 1),
        ("News interval", s.news_minutes, 1),
        ("Maximum daily buys", s.max_daily_buys, 0),
        ("Maximum positions", s.max_positions, 0),
        ("News lookback", s.news_lookback_hours, 1),
        ("LLM reviews per day", s.llm_max_reviews_per_day, 0),
    ):
        if not isinstance(value, int) or value < low:
            errors.append(f"{label} must be a whole number of at least {low}.")
    for name, rule in s.slippage_categories.items():
        if rule.get("mode") not in ("abs", "pct"):
            errors.append(f"Slippage category '{name}' must use abs or pct.")
        try:
            if parse_decimal(rule.get("value"), f"Slippage for '{name}'") < 0:
                errors.append(f"Slippage for '{name}' cannot be negative.")
        except ValueError as exc:
            errors.append(str(exc))
    if s.default_category not in s.slippage_categories:
        errors.append("The default slippage category does not exist.")
    for ticker, category in s.ticker_categories.items():
        if category not in s.slippage_categories:
            errors.append(f"{ticker} uses unknown slippage category '{category}'.")
        try:
            normalize_ticker(ticker)
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def load_settings(db: Database) -> Settings:
    rows = db.conn().execute("SELECT key, value FROM settings").fetchall()
    stored = {row["key"]: json.loads(row["value"]) for row in rows}
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in stored.items() if k in known})


def save_settings(db: Database, new: Settings, clock: Clock) -> None:
    errors = validate(new)
    if errors:
        raise ValueError("\n".join(errors))
    old = load_settings(db)
    now = to_iso(clock())
    with db.tx() as conn:
        if new.start != old.start:
            if conn.execute("SELECT 1 FROM trades LIMIT 1").fetchone():
                raise ValueError(
                    "The starting balance is locked once trades exist, so the accounts stay "
                    "comparable. Use Reset Experiment to start again with a new balance.")
            conn.execute("DELETE FROM snapshots")
        old_values, new_values = asdict(old), asdict(new)
        for key, value in new_values.items():
            encoded = json.dumps(value, sort_keys=True)
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, encoded))
            previous = json.dumps(old_values[key], sort_keys=True)
            if previous != encoded:
                conn.execute(
                    "INSERT INTO settings_audit(ts, key, old_value, new_value) VALUES (?, ?, ?, ?)",
                    (now, key, previous, encoded))
