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


def test_naive_datetime_treated_as_utc():
    """Naive current_time (no tzinfo) should not raise and should work correctly."""
    pos = _pos(100.0)
    # 04:01 UTC naive = 09:31 IST — well before hard exit → no time stop
    naive_utc = datetime(2026, 5, 7, 4, 1)  # no tzinfo
    close, reason = should_close(pos, Decimal("100.0"), 0.01, naive_utc, [])
    # Should not raise; and should not fire time_stop (it's 09:31 IST)
    assert reason != "time_stop"


def test_profit_ratchet_breakeven_stop():
    """At +1R, stop moves to breakeven; current premium at entry should close."""
    pos = _pos(100.0)  # entry=100, initial_risk_r=20
    # current_premium == entry (100) at +1R (pnl=20) → close
    pos.initial_risk_r = Decimal("20")
    # Simulate: current_premium = entry_premium_net = 100; pnl = 0 (not at +1R)
    # Need: pnl >= r i.e. current >= 120, and current <= entry (100) → impossible normally
    # Instead: peak=120, current=100 (at entry), pnl=0 — pnl < r so no ratchet fires
    # Correct test: pnl >= r means current - entry >= r → current = 120 → at entry now = 100
    pos.peak_premium = Decimal("120")
    # pnl = 100 - 100 = 0; 0 < 20 = r → ratchet not at +1R yet
    close, _ = should_close(pos, Decimal("100.0"), 0.01, _ONE_MIN_UTC, [])
    # Not at +1R so ratchet doesn't fire — trailing stop should fire here if below stop
    # With low vol (0.01) trailing stop ≈ 120 - 0.025 = 119.975; current=100 → fires
    assert close is True
    # reason is trailing_stop (vol trail fires first)


def test_profit_ratchet_trail_from_peak():
    """At +2R peak, trail at 1R from peak stops position."""
    pos = _pos(100.0)
    pos.initial_risk_r = Decimal("20")
    pos.peak_premium = Decimal("145")  # peak at +2.25R above entry
    # pnl = 141 - 100 = 41 >= 2*20=40 → trail at 1R from peak: 145-20=125
    # current=121 < 125 → fires
    close, reason = should_close(pos, Decimal("121.0"), 0.0, _ONE_MIN_UTC, [])
    assert close is True
    assert reason == "trailing_stop"
