"""Tests for the lookahead-bias detector.

Verify the guard:
  * Lets through queries at-or-before the marked virtual time.
  * Raises on queries strictly in the future.
  * Tracks stats correctly.
  * Honours the ``raise_on_violation=False`` mode.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.quant.backtest.checks.lookahead import (
    LookaheadGuard,
    LookaheadViolation,
)


def _ts(min_offset: int = 0) -> datetime:
    return datetime(2026, 4, 27, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=min_offset)


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_passes_through_when_query_at_virtual_time():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner)
    guard.mark_now(_ts(0))
    result = await guard.checked_get(uuid.uuid4(), _ts(0))
    assert result == "bundle"
    assert inner.await_count == 1


@pytest.mark.asyncio
async def test_guard_passes_through_when_query_before_virtual_time():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner)
    guard.mark_now(_ts(10))  # 09:40
    result = await guard.checked_get(uuid.uuid4(), _ts(5))  # 09:35
    assert result == "bundle"


@pytest.mark.asyncio
async def test_guard_passes_through_when_no_mark_yet():
    """No mark → no virtual time to compare → the guard is permissive."""
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner)
    result = await guard.checked_get(uuid.uuid4(), _ts(100))
    assert result == "bundle"


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_raises_on_strict_lookahead():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner)
    guard.mark_now(_ts(0))
    with pytest.raises(LookaheadViolation, match="Lookahead detected"):
        await guard.checked_get(uuid.uuid4(), _ts(1))  # 1 min in the future


@pytest.mark.asyncio
async def test_guard_violation_message_includes_delta_and_timestamps():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner)
    guard.mark_now(_ts(0))
    with pytest.raises(LookaheadViolation) as exc_info:
        await guard.checked_get(uuid.uuid4(), _ts(60))
    msg = str(exc_info.value)
    assert "Lookahead detected" in msg
    # Delta = 60 minutes = 3600 seconds
    assert "3600" in msg or "3600.000" in msg


@pytest.mark.asyncio
async def test_guard_does_not_invoke_wrapped_on_violation():
    """When the guard raises, the wrapped callable must not be called."""
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner)
    guard.mark_now(_ts(0))
    with pytest.raises(LookaheadViolation):
        await guard.checked_get(uuid.uuid4(), _ts(5))
    assert inner.await_count == 0


@pytest.mark.asyncio
async def test_guard_tolerance_allows_small_lookahead():
    """A non-zero tolerance permits queries up to the slack budget."""
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner, tolerance_seconds=120.0)  # 2 min
    guard.mark_now(_ts(0))
    # 1 minute future is within tolerance
    result = await guard.checked_get(uuid.uuid4(), _ts(1))
    assert result == "bundle"
    # 3 minutes is over the budget
    with pytest.raises(LookaheadViolation):
        await guard.checked_get(uuid.uuid4(), _ts(3))


@pytest.mark.asyncio
async def test_guard_no_raise_mode_logs_but_does_not_throw():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner, raise_on_violation=False)
    guard.mark_now(_ts(0))
    # No exception even though this is a violation
    result = await guard.checked_get(uuid.uuid4(), _ts(5))
    assert result == "bundle"
    assert guard.stats().n_violations == 1


# ---------------------------------------------------------------------------
# Stats observability
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_count_calls_and_violations():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner, raise_on_violation=False)
    guard.mark_now(_ts(0))
    # 2 OK calls
    await guard.checked_get(uuid.uuid4(), _ts(0))
    await guard.checked_get(uuid.uuid4(), _ts(-5))
    # 1 violation
    await guard.checked_get(uuid.uuid4(), _ts(10))
    s = guard.stats()
    assert s.n_calls == 3
    assert s.n_violations == 1


@pytest.mark.asyncio
async def test_stats_max_lookahead_seconds_tracks_largest():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner, raise_on_violation=False)
    guard.mark_now(_ts(0))
    await guard.checked_get(uuid.uuid4(), _ts(1))   # +60s
    await guard.checked_get(uuid.uuid4(), _ts(10))  # +600s — biggest
    await guard.checked_get(uuid.uuid4(), _ts(5))   # +300s
    s = guard.stats()
    assert s.max_lookahead_seconds == 600.0


@pytest.mark.asyncio
async def test_reset_stats_zeros_counters():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner, raise_on_violation=False)
    guard.mark_now(_ts(0))
    await guard.checked_get(uuid.uuid4(), _ts(5))
    guard.reset_stats()
    s = guard.stats()
    assert s.n_calls == 0
    assert s.n_violations == 0
    assert s.max_lookahead_seconds == 0.0


@pytest.mark.asyncio
async def test_stats_is_a_copy_not_a_reference():
    inner = AsyncMock(return_value="bundle")
    guard = LookaheadGuard(inner, raise_on_violation=False)
    s1 = guard.stats()
    guard.mark_now(_ts(0))
    await guard.checked_get(uuid.uuid4(), _ts(5))
    # The previously-returned snapshot is unchanged
    assert s1.n_calls == 0


# ---------------------------------------------------------------------------
# LookaheadViolation is an AssertionError
# ---------------------------------------------------------------------------

def test_lookahead_violation_subclasses_assertion_error():
    """Tests that catch ``AssertionError`` will catch this too."""
    assert issubclass(LookaheadViolation, AssertionError)


# ---------------------------------------------------------------------------
# Clock-aware mode (post-review wiring)
# ---------------------------------------------------------------------------

class _StubClock:
    def __init__(self, t):
        self._t = t

    def now(self):
        return self._t


@pytest.mark.asyncio
async def test_clock_aware_guard_uses_clock_now_as_marker():
    inner = AsyncMock(return_value="bundle")
    clock = _StubClock(_ts(0))
    guard = LookaheadGuard(inner, clock=clock)
    # No mark_now call — guard pulls from clock automatically.
    with pytest.raises(LookaheadViolation):
        await guard.checked_get(uuid.uuid4(), _ts(5))


@pytest.mark.asyncio
async def test_clock_aware_guard_passes_when_query_at_clock_now():
    inner = AsyncMock(return_value="bundle")
    clock = _StubClock(_ts(0))
    guard = LookaheadGuard(inner, clock=clock)
    result = await guard.checked_get(uuid.uuid4(), _ts(0))
    assert result == "bundle"


@pytest.mark.asyncio
async def test_clock_aware_guard_advances_with_clock():
    """As the clock advances, more queries become permissible."""
    inner = AsyncMock(return_value="bundle")
    clock = _StubClock(_ts(0))
    guard = LookaheadGuard(inner, clock=clock)
    # At t=0, query for t=5 fails
    with pytest.raises(LookaheadViolation):
        await guard.checked_get(uuid.uuid4(), _ts(5))
    # Advance the clock; same query now passes
    clock._t = _ts(10)
    result = await guard.checked_get(uuid.uuid4(), _ts(5))
    assert result == "bundle"


@pytest.mark.asyncio
async def test_explicit_mark_now_takes_precedence_over_clock():
    """When both are provided, mark_now wins (useful for pinning in tests)."""
    inner = AsyncMock(return_value="bundle")
    clock = _StubClock(_ts(100))  # clock far in the future
    guard = LookaheadGuard(inner, clock=clock)
    guard.mark_now(_ts(0))  # explicit pin to 0
    with pytest.raises(LookaheadViolation):
        await guard.checked_get(uuid.uuid4(), _ts(5))
