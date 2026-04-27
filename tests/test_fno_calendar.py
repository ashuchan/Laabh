"""Tests for F&O expiry calendar — SEBI Sept-2025 rules."""
from __future__ import annotations

from datetime import date

import pytest

from src.fno.calendar import (
    expiry_days_remaining,
    get_near_expiry,
    next_weekly_expiry,
    prev_trading_day,
    trading_days_remaining,
)


def test_nifty_expiry_is_tuesday() -> None:
    # Any Monday → next expiry should be the following Tuesday
    monday = date(2026, 4, 27)  # Monday
    expiry = next_weekly_expiry("NIFTY", reference=monday)
    assert expiry.weekday() == 1, f"Expected Tuesday, got {expiry} ({expiry.strftime('%A')})"


def test_nifty_expiry_on_tuesday_itself_returns_next_tuesday() -> None:
    tuesday = date(2026, 4, 28)  # Already a Tuesday
    expiry = next_weekly_expiry("NIFTY", reference=tuesday)
    # reference is today; expiry must be AFTER today
    assert expiry > tuesday
    assert expiry.weekday() == 1


def test_sensex_expiry_is_thursday() -> None:
    monday = date(2026, 4, 27)
    expiry = next_weekly_expiry("SENSEX", reference=monday)
    assert expiry.weekday() == 3, f"Expected Thursday, got {expiry}"


def test_stock_expiry_is_last_tuesday_of_month() -> None:
    # April 2026: last Tuesday is April 28
    ref = date(2026, 4, 1)
    expiry = next_weekly_expiry("RELIANCE", reference=ref)
    assert expiry == date(2026, 4, 28)


def test_stock_expiry_past_last_tuesday_rolls_to_next_month() -> None:
    # After April's last Tuesday → should give May's last Tuesday
    ref = date(2026, 4, 29)  # Wednesday after April 28
    expiry = next_weekly_expiry("RELIANCE", reference=ref)
    assert expiry.month == 5
    assert expiry.weekday() == 1  # Tuesday


def test_holiday_shifts_expiry_to_previous_trading_day() -> None:
    # Make the normal expiry day a holiday
    tuesday = date(2026, 4, 28)
    expiry = next_weekly_expiry("NIFTY", reference=date(2026, 4, 27), holidays=[tuesday])
    # Should have shifted to Monday 2026-04-27
    assert expiry == date(2026, 4, 27)


def test_prev_trading_day_skips_weekend() -> None:
    monday = date(2026, 4, 27)
    prev = prev_trading_day(monday, frozenset())
    assert prev == date(2026, 4, 24)  # Friday


def test_prev_trading_day_skips_holidays() -> None:
    monday = date(2026, 4, 27)
    friday = date(2026, 4, 24)
    prev = prev_trading_day(monday, frozenset([friday]))
    assert prev == date(2026, 4, 23)  # Thursday


def test_expiry_days_remaining() -> None:
    today = date(2026, 4, 27)
    expiry = date(2026, 4, 30)
    assert expiry_days_remaining(expiry, reference=today) == 3


def test_trading_days_remaining_excludes_weekends() -> None:
    today = date(2026, 4, 27)  # Monday
    expiry = date(2026, 5, 1)  # Friday
    count = trading_days_remaining(expiry, reference=today)
    # Tue, Wed, Thu, Fri = 4 trading days
    assert count == 4


def test_get_near_expiry_within_window() -> None:
    monday = date(2026, 4, 27)
    near = get_near_expiry("NIFTY", max_days=3, reference=monday)
    # Next Tuesday (April 28) is 1 day away → within 3
    assert near == date(2026, 4, 28)


def test_get_near_expiry_outside_window_returns_none() -> None:
    monday = date(2026, 4, 27)
    near = get_near_expiry("NIFTY", max_days=0, reference=monday)
    assert near is None
