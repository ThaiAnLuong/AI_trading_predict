"""Time handling. Stored timestamps are always UTC; display is always local.

Stored format is fixed-width ISO 8601 ("2026-11-04T15:30:00.000000Z"), so text
comparison in SQL matches chronological order.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime cannot be stored")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone()


def local_str(iso: str | None) -> str:
    """Format a stored timestamp for display in the computer's local time."""
    if not iso:
        return "never"
    return to_local(from_iso(iso)).strftime("%Y-%m-%d %H:%M")


def local_date(dt: datetime) -> date:
    return to_local(dt).date()


def local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day).astimezone()


def period_start(now: datetime, period: str) -> datetime:
    """Start of the current limit period: local midnight, or Monday 00:00 local."""
    today = local_date(now)
    if period == "daily":
        return local_midnight(today)
    if period == "weekly":
        return local_midnight(today - timedelta(days=today.weekday()))
    raise ValueError(f"unknown reset period: {period}")


def next_reset(now: datetime, period: str) -> datetime:
    start = local_date(period_start(now, period))
    return local_midnight(start + timedelta(days=1 if period == "daily" else 7))


def age_minutes(iso: str, now: datetime) -> float:
    return (now - from_iso(iso)).total_seconds() / 60
