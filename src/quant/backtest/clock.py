"""Virtual time abstraction for the backtest harness.

The orchestrator's main loop treats the clock as an opaque time source: it
calls ``clock.now()`` and ``clock.sleep_until_next_tick()``. In live mode
those map to ``datetime.now(timezone.utc)`` and ``asyncio.sleep(...)``. In
backtest mode this class provides a deterministic, advancing virtual clock
so the same loop can replay a historical day without wall-clock waits.

Decision Note (clock semantics):
  * All internal state is IST-aware (``pytz.timezone("Asia/Kolkata")``) since
    Indian market hours are defined in IST. ``now()`` returns IST; callers
    that want UTC should ``.astimezone(timezone.utc)``.
  * Holidays / weekends: when the configured trading date is non-trading,
    ``is_market_open()`` returns False for every value of ``now()``, and
    ``remaining_seconds()`` returns 0. This is the spec's "Holiday handling"
    acceptance criterion. Callers can gate the entire day on
    ``is_market_open()`` at the top of the loop.
  * ``sleep_until_next_tick()`` does *not* actually sleep — the suffix
    "_until_next_tick" reflects what the live equivalent does (asyncio.sleep
    until the next polling tick). In backtest mode it advances virtual time
    by ``tick_seconds`` deterministically.
  * Tick alignment: the clock is initialised at ``market_open`` so the first
    ``now()`` is exactly the open. After N ``sleep_until_next_tick()`` calls
    the clock reads ``market_open + N * tick_seconds``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

import pytz


_IST = pytz.timezone("Asia/Kolkata")
# Standard NSE intraday session.
_DEFAULT_MARKET_OPEN = time(9, 15)
_DEFAULT_MARKET_CLOSE = time(15, 30)
# Matches the live ``laabh_quant_hard_exit_time`` default — kept here so the
# clock can answer ``is_after_hard_exit()`` without coupling to settings.
_DEFAULT_HARD_EXIT = time(14, 30)
_DEFAULT_TICK_SECONDS = 180


@dataclass
class BacktestClock:
    """Deterministic virtual clock for one trading day.

    Args:
        trading_date: Calendar date being replayed.
        market_open: Session open in IST (default 09:15).
        market_close: Session close in IST (default 15:30).
        tick_seconds: Granularity of ``sleep_until_next_tick`` advancement.
        hard_exit_time: IST cut-off for new entries (default 14:30).
        holidays: Set of NSE/BSE holiday dates. If ``trading_date`` is in
            this set or is a weekend, the clock reports ``is_market_open()``
            == False unconditionally.
    """

    trading_date: date
    market_open: time = _DEFAULT_MARKET_OPEN
    market_close: time = _DEFAULT_MARKET_CLOSE
    tick_seconds: int = _DEFAULT_TICK_SECONDS
    hard_exit_time: time = _DEFAULT_HARD_EXIT
    holidays: frozenset[date] = field(default_factory=frozenset)

    # Internal: IST-aware "current" virtual time.
    _now_ist: datetime = field(init=False)

    def __post_init__(self) -> None:
        if self.tick_seconds <= 0:
            raise ValueError(f"tick_seconds must be positive, got {self.tick_seconds}")
        if self.market_close <= self.market_open:
            raise ValueError(
                f"market_close ({self.market_close}) must be after "
                f"market_open ({self.market_open})"
            )
        # Coerce holidays into a frozenset of date objects (idempotent if
        # caller already passed one) — accepts any Iterable[date].
        if not isinstance(self.holidays, frozenset):
            self.holidays = frozenset(self.holidays)
        self._now_ist = self._localize(self.trading_date, self.market_open)

    # ------------------------------------------------------------------
    # Core API (called by orchestrator)
    # ------------------------------------------------------------------

    def now(self) -> datetime:
        """Return the current virtual time as an IST-aware datetime."""
        return self._now_ist

    def sleep_until_next_tick(self) -> None:
        """Advance virtual time by one tick. Does not actually sleep."""
        self._now_ist = self._now_ist + timedelta(seconds=self.tick_seconds)

    def advance(self, seconds: float) -> None:
        """Advance virtual time by ``seconds``.

        Distinct from ``sleep_until_next_tick`` (which advances by the
        configured ``tick_seconds``). Used by the ``BacktestClockAdapter``
        when the orchestrator drives time progression directly via the
        legacy ``as_of``/``current_time += poll_delta`` path.
        """
        self._now_ist = self._now_ist + timedelta(seconds=float(seconds))

    def is_market_open(self) -> bool:
        """True iff the current virtual time is within market hours on a trading day.

        Returns False for any virtual time on a holiday or weekend.
        """
        if not self._is_trading_day(self.trading_date):
            return False
        t = self._now_ist.time()
        return self.market_open <= t < self.market_close

    def is_after_hard_exit(self) -> bool:
        """True iff the virtual clock has passed the hard-exit cut-off."""
        return self._now_ist.time() >= self.hard_exit_time

    def remaining_seconds(self) -> int:
        """Seconds remaining until ``market_close``. 0 on holidays / past close."""
        if not self._is_trading_day(self.trading_date):
            return 0
        close_dt = self._localize(self.trading_date, self.market_close)
        diff = (close_dt - self._now_ist).total_seconds()
        return max(0, int(diff))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def session_open(self) -> datetime:
        """IST-aware datetime of session open for ``trading_date``."""
        return self._localize(self.trading_date, self.market_open)

    def session_close(self) -> datetime:
        """IST-aware datetime of session close for ``trading_date``."""
        return self._localize(self.trading_date, self.market_close)

    def _is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    @staticmethod
    def _localize(d: date, t: time) -> datetime:
        """Build an IST-aware datetime from a date and a naive time."""
        return _IST.localize(datetime.combine(d, t))


def trading_days_between(
    start: date,
    end: date,
    *,
    holidays: Iterable[date] = (),
) -> list[date]:
    """Return inclusive trading-day range [start, end], skipping weekends + holidays.

    Used by ``BacktestRunner.run_range`` to enumerate dates. Defined here
    rather than in calendar.py because the runner only needs simple weekday +
    holiday filtering — F&O expiry semantics are out of scope.
    """
    if end < start:
        return []
    holiday_set = frozenset(holidays)
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in holiday_set:
            out.append(d)
        d = d + timedelta(days=1)
    return out
