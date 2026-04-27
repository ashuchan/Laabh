"""NSE/BSE expiry calendar — SEBI Sept-2025 reform aware.

Key rules:
* Nifty 50 weekly expiry: Tuesday
* BSE Sensex weekly expiry: Thursday
* Bank Nifty / Fin Nifty / Midcap Nifty: monthly only (last Tuesday)
* All NSE monthly contracts: last Tuesday of the month
* BSE monthly contracts: last Thursday of the month
* If the expiry day is a market holiday → shift to the PREVIOUS trading day

The calendar does NOT hard-code expiry dates; it resolves them dynamically
using the holiday list stored in `system_config`.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

from loguru import logger

# Weekday constants (Monday=0 … Sunday=6)
_TUESDAY = 1
_THURSDAY = 3

# Indices that have weekly F&O expiries
_NSE_WEEKLY_INDICES = {"NIFTY 50", "NIFTY50", "NIFTY"}
_BSE_WEEKLY_INDICES = {"SENSEX"}


def _is_holiday(d: date, holidays: frozenset[date]) -> bool:
    return d in holidays


def _is_trading_day(d: date, holidays: frozenset[date]) -> bool:
    return d.weekday() < 5 and not _is_holiday(d, holidays)


def prev_trading_day(d: date, holidays: frozenset[date]) -> date:
    """Return the most recent trading day strictly before `d`."""
    candidate = d - timedelta(days=1)
    while not _is_trading_day(candidate, holidays):
        candidate -= timedelta(days=1)
    return candidate


def _expiry_or_prev(d: date, holidays: frozenset[date]) -> date:
    """Return `d` if it is a trading day; otherwise the previous trading day."""
    if _is_trading_day(d, holidays):
        return d
    return prev_trading_day(d, holidays)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of `weekday` in the given month."""
    # Start from the last day of the month and walk back
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _next_weekday_on_or_after(d: date, weekday: int) -> date:
    """Return the first occurrence of `weekday` on or after `d`."""
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset)


def next_weekly_expiry(
    symbol: str,
    reference: date | None = None,
    holidays: Sequence[date] | None = None,
) -> date:
    """Return the next weekly (or monthly-only) expiry date for `symbol`.

    Args:
        symbol: Instrument symbol (e.g. "NIFTY", "SENSEX", "HDFCBANK").
        reference: The reference date (defaults to today).
        holidays: List of NSE/BSE market holidays.

    Returns:
        The upcoming expiry date, possibly shifted to the previous trading day
        if the computed expiry falls on a holiday.
    """
    today = reference or date.today()
    holiday_set: frozenset[date] = frozenset(holidays or [])

    sym_upper = symbol.upper()

    # Indices with weekly expiry
    if sym_upper in _NSE_WEEKLY_INDICES:
        # Next Tuesday on or after tomorrow
        candidate = _next_weekday_on_or_after(today + timedelta(days=1), _TUESDAY)
        return _expiry_or_prev(candidate, holiday_set)

    if sym_upper in _BSE_WEEKLY_INDICES:
        candidate = _next_weekday_on_or_after(today + timedelta(days=1), _THURSDAY)
        return _expiry_or_prev(candidate, holiday_set)

    # Stock F&O: monthly — last Tuesday of the month (NSE)
    # Try current month first; if already past, use next month
    year, month = today.year, today.month
    candidate = _last_weekday_of_month(year, month, _TUESDAY)
    if candidate <= today:
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        candidate = _last_weekday_of_month(year, month, _TUESDAY)

    return _expiry_or_prev(candidate, holiday_set)


def expiry_days_remaining(expiry: date, reference: date | None = None) -> int:
    """Calendar days until expiry (0 on expiry day, negative if past)."""
    today = reference or date.today()
    return (expiry - today).days


def trading_days_remaining(
    expiry: date,
    reference: date | None = None,
    holidays: Sequence[date] | None = None,
) -> int:
    """Count of trading days from today (exclusive) up to and including `expiry`."""
    today = reference or date.today()
    holiday_set: frozenset[date] = frozenset(holidays or [])
    count = 0
    cursor = today + timedelta(days=1)
    while cursor <= expiry:
        if _is_trading_day(cursor, holiday_set):
            count += 1
        cursor += timedelta(days=1)
    return count


def get_near_expiry(
    symbol: str,
    max_days: int = 3,
    reference: date | None = None,
    holidays: Sequence[date] | None = None,
) -> date | None:
    """Return the next expiry if it is within `max_days` calendar days, else None."""
    today = reference or date.today()
    expiry = next_weekly_expiry(symbol, reference=today, holidays=holidays)
    if expiry_days_remaining(expiry, reference=today) <= max_days:
        return expiry
    logger.debug(f"{symbol}: nearest expiry {expiry} is beyond {max_days}-day window")
    return None
