"""Tests for the token-bucket rate limiter used by the LLM backfill."""
from __future__ import annotations

import asyncio
import time

import pytest

from src.utils.rate_limit import TokenBucketRateLimiter


def test_max_per_min_is_stored() -> None:
    rl = TokenBucketRateLimiter(45)
    assert rl.max_per_min == 45


def test_floor_at_one_per_minute() -> None:
    # A misconfigured 0 must not deadlock the acquirer forever.
    rl = TokenBucketRateLimiter(0)
    assert rl.max_per_min == 1


@pytest.mark.asyncio
async def test_first_n_acquires_are_immediate() -> None:
    rl = TokenBucketRateLimiter(5)
    t0 = time.monotonic()
    for _ in range(5):
        await rl.acquire()
    elapsed = time.monotonic() - t0
    # 5 acquires within the budget should finish in well under a second.
    assert elapsed < 0.2, f"unexpectedly slow: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_reset_clears_stamps() -> None:
    rl = TokenBucketRateLimiter(2)
    await rl.acquire()
    await rl.acquire()
    rl.reset()
    # After reset both slots are free again — these should complete
    # without waiting for the 60s window to age out.
    t0 = time.monotonic()
    await rl.acquire()
    await rl.acquire()
    assert time.monotonic() - t0 < 0.2


@pytest.mark.asyncio
async def test_excess_acquire_actually_blocks() -> None:
    """Confirm the limiter waits when the per-minute budget is exhausted.

    Sets a budget of 2/min, takes both slots immediately, then verifies a
    3rd acquire is still pending after a short timeout — i.e. the limiter
    is genuinely waiting for the oldest stamp to age out of the 60-second
    window, not returning early. Without this test, a regression that
    accidentally bypassed the lock (e.g. `if len(self._stamps) <= self._max`
    off-by-one) would slip through.
    """
    rl = TokenBucketRateLimiter(2)
    await rl.acquire()
    await rl.acquire()

    with pytest.raises(asyncio.TimeoutError):
        # 3rd would block until the oldest stamp ages out (~60s).
        # 0.3s timeout proves it's at least trying to wait.
        await asyncio.wait_for(rl.acquire(), timeout=0.3)


@pytest.mark.asyncio
async def test_acquire_releases_after_window_clears(monkeypatch) -> None:
    """The waiting acquire returns once a stamp ages out of the window.

    We can't wait 60s in a test, so we fast-forward monotonic time via
    monkeypatch. After advancing past the 60-second mark the limiter
    must let the queued acquire through.
    """
    import src.utils.rate_limit as rl_mod

    # Build a fake clock the limiter will read from. Start at t=0; each
    # tick can be advanced by mutating ``state['now']``.
    state = {"now": 0.0}

    def fake_monotonic() -> float:
        return state["now"]

    monkeypatch.setattr(rl_mod._time, "monotonic", fake_monotonic)

    rl = TokenBucketRateLimiter(2)
    await rl.acquire()      # stamp at t=0
    await rl.acquire()      # stamp at t=0

    # Advance virtual time past the 60-second window AND make
    # asyncio.sleep a no-op so the limiter's internal sleep loop
    # doesn't burn wall-clock time.
    state["now"] = 61.0
    real_sleep = asyncio.sleep

    async def instant_sleep(seconds, *args, **kwargs):
        # Forward only a `0` sleep so the event loop yields once.
        return await real_sleep(0)

    monkeypatch.setattr(rl_mod.asyncio, "sleep", instant_sleep)

    # 3rd acquire should now succeed promptly because both prior stamps
    # are >60s old.
    await asyncio.wait_for(rl.acquire(), timeout=0.5)
