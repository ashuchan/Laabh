"""Tests for intraday manager — Phase 4 position lifecycle."""
from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal

import pytest

from src.fno.intraday_manager import (
    IntradayState,
    OpenPosition,
    apply_tick,
    check_stop_loss,
    check_target,
    is_entry_allowed,
    should_hard_exit,
    update_trailing_stop,
)


def _now_at(hour: int, minute: int) -> datetime:
    """Create a naive-ish datetime in IST-equivalent for testing."""
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime(2026, 4, 27, hour, minute, tzinfo=ist)


def _position(
    entry: float = 100.0,
    stop: float = 50.0,
    target: float = 200.0,
) -> OpenPosition:
    return OpenPosition(
        instrument_id="test-id",
        symbol="NIFTY",
        strategy_name="long_call",
        option_type="CE",
        strike=Decimal("22000"),
        entry_price=Decimal(str(entry)),
        stop_price=Decimal(str(stop)),
        target_price=Decimal(str(target)),
        lots=1,
        lot_size=50,
    )


# ---------------------------------------------------------------------------
# Entry gating
# ---------------------------------------------------------------------------

def test_entry_allowed_normal_market() -> None:
    state = IntradayState()
    ok, reason = is_entry_allowed(
        _now_at(10, 0), "inst-1", state,
        no_entry_minutes=30, max_open_positions=3,
    )
    assert ok is True
    assert reason == ""


def test_entry_blocked_pre_market_gate() -> None:
    state = IntradayState()
    ok, reason = is_entry_allowed(
        _now_at(9, 20), "inst-1", state,
        no_entry_minutes=30,
    )
    assert ok is False
    assert "pre_market_gate" in reason


def test_entry_blocked_past_hard_exit() -> None:
    state = IntradayState()
    ok, reason = is_entry_allowed(
        _now_at(14, 45), "inst-1", state,
        hard_exit_time=time(14, 30),
    )
    assert ok is False
    assert "past_hard_exit" in reason


def test_entry_blocked_max_positions() -> None:
    state = IntradayState()
    state.open_positions = [_position(), _position(), _position()]
    ok, reason = is_entry_allowed(
        _now_at(10, 0), "inst-1", state,
        max_open_positions=3,
    )
    assert ok is False
    assert "max_positions" in reason


def test_entry_blocked_cooldown() -> None:
    state = IntradayState()
    cooldown_until = _now_at(11, 0)
    state.cooldowns["inst-1"] = cooldown_until
    ok, reason = is_entry_allowed(
        _now_at(10, 0), "inst-1", state,
        no_entry_minutes=0,  # disable pre-market gate so cooldown is reached
    )
    assert ok is False
    assert "cooldown" in reason


def test_entry_blocked_hard_exited() -> None:
    state = IntradayState()
    state.hard_exited = True
    ok, reason = is_entry_allowed(_now_at(10, 0), "inst-1", state)
    assert ok is False
    assert "hard_exit_triggered" in reason


# ---------------------------------------------------------------------------
# Stop loss and target checks
# ---------------------------------------------------------------------------

def test_check_stop_loss_hit() -> None:
    pos = _position(entry=100.0, stop=50.0)
    assert check_stop_loss(pos, Decimal("45")) is True


def test_check_stop_loss_not_hit() -> None:
    pos = _position(entry=100.0, stop=50.0)
    assert check_stop_loss(pos, Decimal("60")) is False


def test_check_stop_loss_exact_stop_price() -> None:
    pos = _position(entry=100.0, stop=50.0)
    assert check_stop_loss(pos, Decimal("50")) is True


def test_check_target_hit() -> None:
    pos = _position(entry=100.0, target=200.0)
    assert check_target(pos, Decimal("210")) is True


def test_check_target_not_hit() -> None:
    pos = _position(entry=100.0, target=200.0)
    assert check_target(pos, Decimal("150")) is False


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------

def test_trailing_stop_activates_on_big_gain() -> None:
    pos = _position(entry=100.0, stop=50.0)
    updated = update_trailing_stop(pos, Decimal("135"), scale_out_pct=0.30, trailing_stop_pct=0.20)
    assert updated is True
    assert pos.trailing_active is True
    assert pos.stop_price > Decimal("50")   # stop was raised


def test_trailing_stop_not_activated_on_small_gain() -> None:
    pos = _position(entry=100.0, stop=50.0)
    updated = update_trailing_stop(pos, Decimal("110"), scale_out_pct=0.30)
    assert updated is False


def test_trailing_stop_peak_tracked() -> None:
    pos = _position(entry=100.0, stop=50.0)
    update_trailing_stop(pos, Decimal("140"), scale_out_pct=0.30)
    update_trailing_stop(pos, Decimal("160"), scale_out_pct=0.30)
    assert pos.peak_price == Decimal("160")


# ---------------------------------------------------------------------------
# apply_tick
# ---------------------------------------------------------------------------

def test_apply_tick_hold() -> None:
    pos = _position(entry=100.0, stop=50.0, target=200.0)
    result = apply_tick(pos, Decimal("120"))
    assert result == "hold"


def test_apply_tick_stop() -> None:
    pos = _position(entry=100.0, stop=50.0, target=200.0)
    result = apply_tick(pos, Decimal("40"))
    assert result == "stop"


def test_apply_tick_target() -> None:
    pos = _position(entry=100.0, stop=50.0, target=200.0)
    result = apply_tick(pos, Decimal("210"))
    assert result == "target"


# ---------------------------------------------------------------------------
# Hard exit
# ---------------------------------------------------------------------------

def test_should_hard_exit_true() -> None:
    assert should_hard_exit(_now_at(14, 35), hard_exit_time=time(14, 30)) is True


def test_should_hard_exit_false() -> None:
    assert should_hard_exit(_now_at(13, 0), hard_exit_time=time(14, 30)) is False


def test_should_hard_exit_exact_time() -> None:
    assert should_hard_exit(_now_at(14, 30), hard_exit_time=time(14, 30)) is True
