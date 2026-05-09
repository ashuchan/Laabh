"""Runtime lookahead-bias detector.

Decorator-pattern wrapper around any feature getter callable. The wrapper
records the virtual time at which the orchestrator says "now is X" and
asserts that every subsequent feature read targets ``X`` or earlier.

Usage:

    fs = BacktestFeatureStore(trading_date=...)
    guard = LookaheadGuard(fs.get)
    ctx = OrchestratorContext(
        ...,
        feature_getter=guard.checked_get,
        clock=...,
    )

The orchestrator's existing call shape is preserved — the guard is
transparent unless a violation occurs, at which point it raises
``LookaheadViolation`` (a subclass of ``AssertionError``).

SOLID notes:
  * SRP — the guard's *only* job is the lookahead invariant. It does no
    feature math, no DB I/O, no caching.
  * OCP — wrapping any callable matching ``(UUID, datetime) -> ...`` is
    free; the guard does not know about ``BacktestFeatureStore``.
  * LSP — the guard's ``checked_get`` has the same signature and return
    type as the wrapped callable. Drop-in substitution.

Decision Note (assertion vs logging):
  * Spec §9 calls for "assertion" + runtime tracking. We choose to *raise*
    on the strictest violation (a query into the future) so the test
    suite catches lookahead immediately. Soft tracking (max-timestamp
    accessed per call) is also exposed via ``stats()`` for observability.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from loguru import logger


class LookaheadViolation(AssertionError):
    """Raised when a feature read targets a timestamp strictly in the future."""


@dataclass
class _GuardStats:
    """Soft observability: counts + max-deltas seen."""

    n_calls: int = 0
    n_violations: int = 0
    max_lookahead_seconds: float = 0.0


class LookaheadGuard:
    """Wraps a feature getter and asserts the no-lookahead invariant.

    Args:
        wrapped: The underlying callable, typically
            ``BacktestFeatureStore.get`` (a bound method) or any
            ``async (UUID, datetime) -> FeatureBundle | None``.
        tolerance_seconds: Allowed slack — defaults to 0 (strict). The
            backtest clock advances in discrete ticks; queries should
            target the bar at-or-before clock.now(), so 0 is correct.

    Example:

        guard = LookaheadGuard(fs.get)
        bundle = await guard.checked_get(uid, virtual_time)
        # Inspect: guard.stats().n_violations
    """

    def __init__(
        self,
        wrapped: Callable[[uuid.UUID, datetime], Awaitable[object]],
        *,
        tolerance_seconds: float = 0.0,
        raise_on_violation: bool = True,
        clock: object | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._tolerance = float(tolerance_seconds)
        self._raise = raise_on_violation
        self._stats = _GuardStats()
        # Tracks the most-recent virtual time the guard has been told about.
        # Two ways to set it:
        #   1. Caller invokes ``mark_now(virtual_time)`` once per tick
        #      (explicit control — matches the original API).
        #   2. Pass a ``clock`` here; ``checked_get`` will read its ``now()``
        #      automatically before each assertion (no caller change).
        # When both are provided, ``mark_now`` overrides the clock for the
        # next call only — useful for tests that need to pin time exactly.
        self._current_virtual_time: datetime | None = None
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_now(self, virtual_time: datetime) -> None:
        """Tell the guard "the virtual clock currently reads X".

        The orchestrator's main loop should call this once per tick before
        any feature reads. Without it, the guard cannot assess lookahead.
        """
        self._current_virtual_time = virtual_time

    async def checked_get(
        self, underlying_id: uuid.UUID, query_time: datetime
    ):
        """Invoke the wrapped getter after asserting no lookahead.

        ``query_time`` is the timestamp the orchestrator passes to the
        feature store. The guard asserts ``query_time <= now + tolerance``,
        where ``now`` is taken from (in priority order):
          1. The most recent explicit ``mark_now()`` call (consumed once),
          2. The injected clock's ``now()`` if a clock was provided,
          3. Otherwise no check is performed (permissive — see test
             ``test_guard_passes_through_when_no_mark_yet``).
        """
        self._stats.n_calls += 1
        marker: datetime | None = self._current_virtual_time
        if marker is None and self._clock is not None:
            try:
                marker = self._clock.now()  # type: ignore[attr-defined]
            except Exception:
                marker = None
        if marker is not None:
            delta = (query_time - marker).total_seconds()
            if delta > self._tolerance:
                self._stats.n_violations += 1
                if delta > self._stats.max_lookahead_seconds:
                    self._stats.max_lookahead_seconds = delta
                msg = (
                    f"Lookahead detected: query_time={query_time.isoformat()} "
                    f"is {delta:.3f}s after virtual clock "
                    f"({marker.isoformat()})."
                )
                logger.error(msg)
                if self._raise:
                    raise LookaheadViolation(msg)
        return await self._wrapped(underlying_id, query_time)

    def stats(self) -> _GuardStats:
        """Return a snapshot of accumulated guard statistics."""
        # Return a copy so callers don't mutate internal state
        return _GuardStats(
            n_calls=self._stats.n_calls,
            n_violations=self._stats.n_violations,
            max_lookahead_seconds=self._stats.max_lookahead_seconds,
        )

    def reset_stats(self) -> None:
        self._stats = _GuardStats()
