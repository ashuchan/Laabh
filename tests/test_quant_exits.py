"""Tests for exit rules."""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.quant.exits import OpenPosition, should_close, _trailing_stop


_ENTRY_UTC = datetime(2026, 5, 7, 4, 0, tzinfo=timezone.utc)   # 09:30 IST
_ONE_MIN_UTC = datetime(2026, 5, 7, 4, 1, tzinfo=timezone.utc)  # 09:31 IST


def _pos(entry: float = 100.0, direction: str = "bullish") -> OpenPosition:
    pos = OpenPosition(
        arm_id="RELIANCE_orb",
        underlying_id="uid",
        direction=direction,
        entry_premium_net=Decimal(str(entry)),
        entry_at=_ENTRY_UTC,
    )
    pos.initial_risk_r = Decimal("20")
    return pos


def _now(hour_utc: int = 6, minute: int = 0) -> datetime:
    return datetime(2026, 5, 7, hour_utc, minute, tzinfo=timezone.utc)


def test_trailing_stop_fires_on_big_drawdown():
    pos = _pos(100.0)
    pos.peak_premium = Decimal("130.0")
    # 1 minute after entry: vol=1.0 → trail = 2.5 × 1.0 × sqrt(1) = 2.5
    # stop = 130 - 2.5 = 127.5; current = 120 → fire
    close, reason = should_close(
        pos, Decimal("120.0"), 1.0, _ONE_MIN_UTC, []
    )
    assert close is True
    assert reason == "trailing_stop"


def test_no_close_above_trailing_stop():
    pos = _pos(100.0)
    pos.peak_premium = Decimal("110.0")
    # 1 min holding, low vol: trail = 2.5 × 0.01 × sqrt(1) = 0.025; stop = 109.975; current = 110 → no fire
    close, _ = should_close(pos, Decimal("110.0"), 0.01, _ONE_MIN_UTC, [])
    assert close is False


def test_time_stop_fires_at_hard_exit():
    pos = _pos(100.0)
    # 09:00 IST = 03:30 UTC; 14:30 IST = 09:00 UTC
    now_utc = datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc)
    close, reason = should_close(pos, Decimal("100.0"), 0.1, now_utc, [])
    assert close is True
    assert reason == "time_stop"


def test_signal_flip_closes_position():
    from src.quant.primitives.base import Signal

    pos = _pos(100.0, direction="bullish")
    bearish_sig = Signal(
        direction="bearish",
        strength=0.8,
        strategy_class="long_put",
        expected_horizon_minutes=15,
        expected_vol_pct=0.01,
    )
    close, reason = should_close(
        pos, Decimal("105.0"), 0.01, _now(),
        [("RELIANCE_orb", bearish_sig)]
    )
    assert close is True
    assert reason == "signal_flip"


def test_signal_flip_ignored_if_strength_low():
    from src.quant.primitives.base import Signal

    pos = _pos(100.0, direction="bullish")
    weak_sig = Signal("bearish", 0.4, "long_put", 15, 0.01)
    close, _ = should_close(
        pos, Decimal("105.0"), 0.01, _now(),
        [("RELIANCE_orb", weak_sig)]
    )
    assert close is False
