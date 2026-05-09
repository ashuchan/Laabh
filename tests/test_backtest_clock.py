"""Unit tests for BacktestClock.

No DB or external services — the clock is pure virtual time.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
import pytz

from src.quant.backtest.clock import BacktestClock, trading_days_between


_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_construct_at_session_open():
    c = BacktestClock(trading_date=date(2026, 4, 27))  # a Monday
    expected = _IST.localize(datetime(2026, 4, 27, 9, 15))
    assert c.now() == expected


def test_construct_with_custom_open_close():
    c = BacktestClock(
        trading_date=date(2026, 4, 27),
        market_open=time(10, 0),
        market_close=time(14, 0),
    )
    assert c.now().time() == time(10, 0)
    assert c.session_close().time() == time(14, 0)


def test_construct_invalid_tick_seconds_rejected():
    with pytest.raises(ValueError, match="tick_seconds must be positive"):
        BacktestClock(trading_date=date(2026, 4, 27), tick_seconds=0)


def test_construct_invalid_close_before_open_rejected():
    with pytest.raises(ValueError, match="market_close"):
        BacktestClock(
            trading_date=date(2026, 4, 27),
            market_open=time(15, 30),
            market_close=time(9, 15),
        )


# ---------------------------------------------------------------------------
# Tick advancement determinism
# ---------------------------------------------------------------------------

def test_sleep_advances_by_tick_seconds_default_180():
    c = BacktestClock(trading_date=date(2026, 4, 27))
    t0 = c.now()
    c.sleep_until_next_tick()
    assert (c.now() - t0).total_seconds() == 180


def test_sleep_advances_by_custom_tick_seconds():
    c = BacktestClock(trading_date=date(2026, 4, 27), tick_seconds=60)
    t0 = c.now()
    for _ in range(5):
        c.sleep_until_next_tick()
    assert (c.now() - t0).total_seconds() == 5 * 60


def test_n_ticks_lands_at_open_plus_n_times_tick():
    c = BacktestClock(trading_date=date(2026, 4, 27), tick_seconds=180)
    expected_open = _IST.localize(datetime(2026, 4, 27, 9, 15))
    for n in range(1, 11):
        c.sleep_until_next_tick()
        assert c.now() == expected_open + timedelta(seconds=n * 180)


# ---------------------------------------------------------------------------
# is_market_open semantics
# ---------------------------------------------------------------------------

def test_is_market_open_at_session_open():
    c = BacktestClock(trading_date=date(2026, 4, 27))
    assert c.is_market_open() is True


def test_is_market_open_just_before_close():
    # Use 30s ticks so we can land exactly at 15:29 — default 180s ticks
    # would overshoot to 15:30 (close, exclusive) and the assertion
    # would be testing the wrong moment.
    c = BacktestClock(trading_date=date(2026, 4, 27), tick_seconds=30)
    while c.now().time() < time(15, 29):
        c.sleep_until_next_tick()
    assert c.now().time() == time(15, 29)
    assert c.is_market_open() is True


def test_is_market_open_at_close_returns_false():
    """Market close (15:30) is exclusive — ``is_market_open`` must be False."""
    c = BacktestClock(
        trading_date=date(2026, 4, 27),
        market_open=time(15, 27),
        market_close=time(15, 30),
        tick_seconds=180,
    )
    assert c.is_market_open() is True
    c.sleep_until_next_tick()  # 15:30 exactly
    assert c.is_market_open() is False


def test_is_market_open_after_close():
    c = BacktestClock(trading_date=date(2026, 4, 27), tick_seconds=600)
    # 15:30 - 9:15 = 6h15m = 22500s = 38 ticks @ 600s each gets us past close
    for _ in range(40):
        c.sleep_until_next_tick()
    assert c.is_market_open() is False


# ---------------------------------------------------------------------------
# Holiday / weekend handling
# ---------------------------------------------------------------------------

def test_holiday_date_is_market_open_always_false():
    holiday = date(2026, 4, 27)  # treat as holiday for this test
    c = BacktestClock(trading_date=holiday, holidays=frozenset({holiday}))
    assert c.is_market_open() is False
    # Even after advancing into market hours
    c.sleep_until_next_tick()
    assert c.is_market_open() is False


def test_saturday_is_market_open_false():
    saturday = date(2026, 5, 2)  # Saturday
    assert saturday.weekday() == 5
    c = BacktestClock(trading_date=saturday)
    assert c.is_market_open() is False


def test_sunday_is_market_open_false():
    sunday = date(2026, 5, 3)  # Sunday
    assert sunday.weekday() == 6
    c = BacktestClock(trading_date=sunday)
    assert c.is_market_open() is False


def test_holidays_iterable_accepted_not_only_frozenset():
    """Caller may pass a list — clock coerces to frozenset internally."""
    holiday = date(2026, 4, 27)
    c = BacktestClock(trading_date=holiday, holidays=[holiday])
    assert c.is_market_open() is False


# ---------------------------------------------------------------------------
# is_after_hard_exit
# ---------------------------------------------------------------------------

def test_is_after_hard_exit_at_open_false():
    c = BacktestClock(trading_date=date(2026, 4, 27))
    assert c.is_after_hard_exit() is False


def test_is_after_hard_exit_at_cutoff_true():
    c = BacktestClock(
        trading_date=date(2026, 4, 27),
        market_open=time(14, 30),
        market_close=time(15, 30),
    )
    # Default hard_exit_time is 14:30
    assert c.is_after_hard_exit() is True


def test_is_after_hard_exit_custom_cutoff():
    c = BacktestClock(
        trading_date=date(2026, 4, 27),
        hard_exit_time=time(15, 0),
    )
    assert c.is_after_hard_exit() is False
    # Advance to 15:00
    while c.now().time() < time(15, 0):
        c.sleep_until_next_tick()
    assert c.is_after_hard_exit() is True


# ---------------------------------------------------------------------------
# remaining_seconds
# ---------------------------------------------------------------------------

def test_remaining_seconds_at_open():
    c = BacktestClock(trading_date=date(2026, 4, 27))
    # 15:30 - 9:15 = 6h15m = 22500s
    assert c.remaining_seconds() == 22500


def test_remaining_seconds_after_one_tick():
    c = BacktestClock(trading_date=date(2026, 4, 27), tick_seconds=300)
    c.sleep_until_next_tick()
    assert c.remaining_seconds() == 22500 - 300


def test_remaining_seconds_after_close_clamped_to_zero():
    c = BacktestClock(
        trading_date=date(2026, 4, 27),
        market_open=time(15, 25),
        market_close=time(15, 30),
        tick_seconds=600,
    )
    c.sleep_until_next_tick()  # well past close
    assert c.remaining_seconds() == 0


def test_remaining_seconds_holiday_is_zero():
    holiday = date(2026, 4, 27)
    c = BacktestClock(trading_date=holiday, holidays={holiday})
    assert c.remaining_seconds() == 0


# ---------------------------------------------------------------------------
# now() returns IST-aware
# ---------------------------------------------------------------------------

def test_now_is_ist_aware():
    c = BacktestClock(trading_date=date(2026, 4, 27))
    n = c.now()
    assert n.tzinfo is not None
    # IST UTC offset: +5:30
    offset = n.utcoffset()
    assert offset == timedelta(hours=5, minutes=30)


def test_now_can_convert_to_utc():
    c = BacktestClock(trading_date=date(2026, 4, 27))
    utc = c.now().astimezone(timezone.utc)
    # 09:15 IST = 03:45 UTC
    assert utc.hour == 3 and utc.minute == 45


# ---------------------------------------------------------------------------
# trading_days_between
# ---------------------------------------------------------------------------

def test_trading_days_between_simple_week():
    # Monday 2026-04-27 → Friday 2026-05-01
    days = trading_days_between(date(2026, 4, 27), date(2026, 5, 1))
    assert len(days) == 5
    assert days[0] == date(2026, 4, 27)
    assert days[-1] == date(2026, 5, 1)


def test_trading_days_between_skips_weekend():
    # Friday 2026-05-01 → Monday 2026-05-04
    days = trading_days_between(date(2026, 5, 1), date(2026, 5, 4))
    assert days == [date(2026, 5, 1), date(2026, 5, 4)]


def test_trading_days_between_skips_holiday():
    holiday = date(2026, 4, 29)  # Wed
    days = trading_days_between(
        date(2026, 4, 27), date(2026, 5, 1), holidays={holiday}
    )
    assert holiday not in days
    assert len(days) == 4


def test_trading_days_between_empty_when_end_before_start():
    assert trading_days_between(date(2026, 5, 1), date(2026, 4, 27)) == []


def test_trading_days_between_single_trading_day():
    days = trading_days_between(date(2026, 4, 27), date(2026, 4, 27))
    assert days == [date(2026, 4, 27)]


def test_trading_days_between_single_holiday_day():
    holiday = date(2026, 4, 27)
    assert trading_days_between(holiday, holiday, holidays={holiday}) == []
