"""Clock abstraction — separates wall-clock from virtual-clock.

The orchestrator depends on this Protocol rather than ``datetime.now`` so the
backtest harness can inject a deterministic virtual clock without modifying
the orchestrator.

``BacktestClock`` (in ``src.quant.backtest.clock``) implements this Protocol
structurally — no ``isinstance`` check is needed; Python's structural typing
handles the substitution at the orchestrator's call sites.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Protocol, runtime_checkable

import pytz


_IST = pytz.timezone("Asia/Kolkata")


@runtime_checkable
class Clock(Protocol):
    """Time-source contract used by the orchestrator.

    All time values are timezone-aware. Live implementations return UTC;
    backtest implementations return IST. Callers normalise as needed.

    Decision Note (no ``is_after_hard_exit`` on the Protocol):
      The orchestrator does the hard-exit comparison inline against
      ``current_time`` (which it advances itself in replay mode). Hoisting
      that check onto the clock would silently break replay-mode backtests
      because the underlying ``BacktestClock`` is *not* advanced by the
      orchestrator's ``current_time += poll_delta`` path. Implementations
      may still expose the method for direct callers (``BacktestClock`` does,
      per spec Task 7); it just isn't part of the Protocol contract.
    """

    def now(self) -> datetime:
        """Return the current time (timezone-aware)."""
        ...

    async def sleep_until_next_tick(self, *, tick_start: datetime, poll_seconds: int) -> None:
        """Block (live) or no-op (backtest) until the next polling tick.

        ``tick_start`` is the timestamp at which the current tick began —
        live implementations sleep ``poll_seconds`` minus elapsed; backtest
        implementations advance virtual time.
        """
        ...

    def advance(self, seconds: float) -> None:
        """Advance virtual time by ``seconds``. No-op on live clocks.

        Used by the orchestrator's legacy ``as_of``-driven replay path:
        when ``current_time`` is advanced manually, downstream consumers
        like ``LookaheadGuard`` need the clock to stay in sync. Live
        clocks have no virtual state to advance; this is a no-op there.
        """
        ...


@dataclass
class LiveClock:
    """Wall-clock implementation. Default for live ``run_loop`` invocations.

    No state — instances are interchangeable. Kept as a dataclass so future
    extensions (e.g. a ``time_offset`` for replay) can be added without
    touching the Protocol.
    """

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep_until_next_tick(
        self, *, tick_start: datetime, poll_seconds: int
    ) -> None:
        elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
        sleep_sec = max(0.0, poll_seconds - elapsed)
        if sleep_sec > 0:
            await asyncio.sleep(sleep_sec)

    def is_after_hard_exit(self, hard_exit_time: time) -> bool:
        return self.now().astimezone(_IST).time() >= hard_exit_time

    def advance(self, seconds: float) -> None:
        """No-op: wall-clock has no virtual state to advance."""
        return None


# ---------------------------------------------------------------------------
# Adapter so BacktestClock (Task 7) satisfies this Protocol structurally
# ---------------------------------------------------------------------------

@dataclass
class BacktestClockAdapter:
    """Wraps ``src.quant.backtest.clock.BacktestClock`` to match the live
    ``Clock`` Protocol exactly.

    ``BacktestClock.sleep_until_next_tick`` takes no args (it advances by a
    fixed ``tick_seconds``); the live Protocol passes ``tick_start`` and
    ``poll_seconds`` for parity. The adapter ignores those — the backtest
    clock is already configured with its own tick size.
    """

    inner: object  # BacktestClock — typed loosely to avoid circular import

    def now(self) -> datetime:
        return self.inner.now()  # type: ignore[attr-defined]

    async def sleep_until_next_tick(
        self, *, tick_start: datetime, poll_seconds: int
    ) -> None:
        # Backtest clock does its own tick advancement; live tick_start /
        # poll_seconds are intentionally ignored.
        self.inner.sleep_until_next_tick()  # type: ignore[attr-defined]

    def is_after_hard_exit(self, hard_exit_time: time) -> bool:
        # BacktestClock has its own hard_exit_time configured at construction
        # but exposes is_after_hard_exit() with no arg. Use that when the
        # times match; otherwise fall back to comparing now().time().
        try:
            inner_hard = getattr(self.inner, "hard_exit_time", None)
            if inner_hard == hard_exit_time:
                return self.inner.is_after_hard_exit()  # type: ignore[attr-defined]
        except Exception:
            pass
        return self.inner.now().time() >= hard_exit_time  # type: ignore[attr-defined]

    def advance(self, seconds: float) -> None:
        """Advance the inner BacktestClock's virtual time by ``seconds``."""
        self.inner.advance(seconds)  # type: ignore[attr-defined]
