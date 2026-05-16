"""Async token-bucket rate limiter for external API budgets.

Used by ``scripts/backfill_llm_features.py`` to stay under Anthropic's
per-tier request-per-minute ceiling without manual ``sleep(2 / k)``
hand-tuning. Mirrors the ``_RateLimiter`` in
``src.quant.backtest.data_loaders.dhan_historical`` — same algorithm,
lifted to a shared module so the Anthropic backfill path and the Dhan
backfill path don't drift apart over time.

Rolling-window token bucket: at most ``max_per_min`` ``acquire()``
returns within any 60-second window. Caller awaits ``acquire`` before
each request; the limiter sleeps until the oldest stamp ages out when
the window is full.
"""
from __future__ import annotations

import asyncio
import time as _time


class TokenBucketRateLimiter:
    """Async token-bucket sized in requests per rolling 60-second window."""

    def __init__(self, max_per_min: int):
        # Force at least 1 so a misconfigured 0 doesn't deadlock forever.
        self._max = max(1, int(max_per_min))
        self._stamps: list[float] = []
        self._lock = asyncio.Lock()

    @property
    def max_per_min(self) -> int:
        return self._max

    async def acquire(self) -> None:
        """Block until a slot is available, then consume it."""
        async with self._lock:
            while True:
                now = _time.monotonic()
                self._stamps = [s for s in self._stamps if now - s < 60.0]
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                # Oldest stamp ages out at +60s; wait that long then re-check.
                wait = 60.0 - (now - self._stamps[0]) + 0.01
                await asyncio.sleep(max(0.01, wait))

    def reset(self) -> None:
        """Drop all stamps. Test-only helper."""
        self._stamps.clear()
