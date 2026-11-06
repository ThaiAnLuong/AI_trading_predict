"""Exact decimal arithmetic for money. Floats are never used for balances."""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
PRICE_STEP = Decimal("0.0001")
QTY_STEP = Decimal("0.0001")
ZERO = Decimal("0")
BASE_CURRENCY = "CAD"


def D(value) -> Decimal:
    """Convert to Decimal, going through str so floats do not leak binary noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).strip().replace(",", ""))


def parse_decimal(text, label: str) -> Decimal:
    """Parse user input, raising ValueError with a readable message."""
    try:
        value = D(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be a number.")
    return value


def cost_cents(value: Decimal) -> Decimal:
    """Round money leaving the account up, so rounding never favours the trader."""
    return value.quantize(CENT, rounding=ROUND_UP)


def proceeds_cents(value: Decimal) -> Decimal:
    """Round money entering the account down, so rounding never favours the trader."""
    return value.quantize(CENT, rounding=ROUND_DOWN)


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value, currency: str = BASE_CURRENCY) -> str:
    if value is None:
        return "n/a"
    return f"{currency} {cents(D(value)):,.2f}"


def fmt_signed(value, currency: str = BASE_CURRENCY) -> str:
    if value is None:
        return "n/a"
    amount = cents(D(value))
    return f"{'+' if amount >= 0 else '-'}{currency} {abs(amount):,.2f}"


def fmt_pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{D(value):+.2f}%"


def fmt_price(value, currency: str = "") -> str:
    if value is None:
        return "n/a"
    text = f"{D(value).quantize(PRICE_STEP):,.4f}"
    return f"{currency} {text}".strip()


def fmt_qty(value) -> str:
    text = f"{D(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text
