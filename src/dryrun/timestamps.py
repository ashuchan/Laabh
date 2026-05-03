"""Timestamp helpers for the dry-run replay orchestrator.

All helpers operate in IST (Asia/Kolkata) and return timezone-aware datetime
objects in UTC for consistency with the rest of the codebase.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytz

_IST = pytz.timezone("Asia/Kolkata")


def ist(d: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Return a UTC datetime for date d at HH:MM:SS IST."""
    naive = datetime(d.year, d.month, d.day, hour, minute, second)
    return _IST.localize(naive).astimezone(pytz.utc)


def scheduled_chain_times(d: date) -> list[datetime]:
    """Return every chain-collection timestamp for date d.

    Tier 1: every 5 minutes from 09:00 to 15:30 IST (inclusive).
    Returns UTC datetimes.
    """
    times: list[datetime] = []
    current = ist(d, 9, 0)
    end = ist(d, 15, 30)
    step = timedelta(minutes=5)
    while current <= end:
        times.append(current)
        current += step
    return times


def scheduled_macro_times(d: date) -> list[datetime]:
    """Return every macro-collection timestamp for date d.

    Macro runs every 15 minutes from 06:00 to 09:00 IST (pre-market window).
    Returns UTC datetimes.
    """
    times: list[datetime] = []
    current = ist(d, 6, 0)
    end = ist(d, 9, 0)
    step = timedelta(minutes=15)
    while current <= end:
        times.append(current)
        current += step
    return times


def minute_range(
    d: date,
    start_hour: int,
    start_minute: int,
    end_hour: int,
    end_minute: int,
) -> list[datetime]:
    """Return one datetime per minute from (d, start_h:start_m) to (d, end_h:end_m) IST.

    Used to drive the Phase 4 intraday tick loop.
    """
    times: list[datetime] = []
    current = ist(d, start_hour, start_minute)
    end = ist(d, end_hour, end_minute)
    step = timedelta(minutes=1)
    while current <= end:
        times.append(current)
        current += step
    return times
