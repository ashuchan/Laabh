"""Tests for src/dryrun/timestamps.py."""
from __future__ import annotations

from datetime import date, timedelta, timezone

import pytest
import pytz

from src.dryrun.timestamps import (
    ist,
    minute_range,
    scheduled_chain_times,
    scheduled_macro_times,
)

_IST = pytz.timezone("Asia/Kolkata")


def test_ist_returns_utc():
    d = date(2026, 4, 23)
    result = ist(d, 9, 15)
    assert result.tzinfo is not None
    # 09:15 IST = 03:45 UTC
    assert result.hour == 3
    assert result.minute == 45


def test_scheduled_chain_times_count():
    d = date(2026, 4, 23)
    times = scheduled_chain_times(d)
    # 09:00 to 15:30 every 5 min = (6*60+30)/5 + 1 = 79 + 1 = 79 entries
    # Actually: (15:30 - 09:00) = 390 min / 5 = 78 intervals + 1 = 79
    assert len(times) == 79
    # All UTC timezone-aware
    assert all(t.tzinfo is not None for t in times)
    # First is 09:00 IST
    first_ist = times[0].astimezone(_IST)
    assert first_ist.hour == 9 and first_ist.minute == 0
    # Last is 15:30 IST
    last_ist = times[-1].astimezone(_IST)
    assert last_ist.hour == 15 and last_ist.minute == 30


def test_scheduled_macro_times():
    d = date(2026, 4, 23)
    times = scheduled_macro_times(d)
    # 06:00 to 09:00 every 15 min = 180/15 + 1 = 13
    assert len(times) == 13
    first_ist = times[0].astimezone(_IST)
    assert first_ist.hour == 6 and first_ist.minute == 0


def test_minute_range_count():
    d = date(2026, 4, 23)
    # 09:15 to 14:30 = 315 minutes + 1 = 316 entries
    ticks = minute_range(d, 9, 15, 14, 30)
    assert len(ticks) == 316
    first_ist = ticks[0].astimezone(_IST)
    assert first_ist.hour == 9 and first_ist.minute == 15
    last_ist = ticks[-1].astimezone(_IST)
    assert last_ist.hour == 14 and last_ist.minute == 30


def test_minute_range_single_point():
    d = date(2026, 4, 23)
    ticks = minute_range(d, 9, 15, 9, 15)
    assert len(ticks) == 1
