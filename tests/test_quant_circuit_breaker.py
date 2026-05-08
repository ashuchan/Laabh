"""Tests for circuit breaker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.quant.circuit_breaker import CircuitState


def _now() -> datetime:
    return datetime(2026, 5, 7, 5, 0, tzinfo=timezone.utc)


def test_lockin_fires_on_gain():
    state = CircuitState(starting_nav=1_000_000.0)
    state.check_and_fire(1_050_001.0, _now())
    assert state.lockin_active is True
    assert state.lockin_fired_at is not None


def test_lockin_not_fired_below_threshold():
    state = CircuitState(starting_nav=1_000_000.0)
    state.check_and_fire(1_040_000.0, _now())
    assert state.lockin_active is False


def test_kill_fires_on_drawdown():
    state = CircuitState(starting_nav=1_000_000.0)
    state.check_and_fire(969_999.0, _now())
    assert state.kill_active is True


def test_kill_not_fired_above_threshold():
    state = CircuitState(starting_nav=1_000_000.0)
    state.check_and_fire(975_000.0, _now())
    assert state.kill_active is False


def test_cooloff_fires_after_n_consecutive_losses():
    state = CircuitState(starting_nav=1_000_000.0)
    now = _now()
    # Default COOLOFF_CONSECUTIVE_LOSSES = 3
    state.record_loss("A_orb", now)
    state.record_loss("A_orb", now)
    assert not state.arm_in_cooloff("A_orb", now)
    state.record_loss("A_orb", now)  # 3rd loss
    assert state.arm_in_cooloff("A_orb", now)


def test_cooloff_expires():
    state = CircuitState(starting_nav=1_000_000.0)
    now = _now()
    for _ in range(3):
        state.record_loss("A_orb", now)
    future = now + timedelta(minutes=31)
    assert not state.arm_in_cooloff("A_orb", future)


def test_win_resets_consecutive_losses():
    state = CircuitState(starting_nav=1_000_000.0)
    now = _now()
    state.record_loss("A_orb", now)
    state.record_loss("A_orb", now)
    state.record_win("A_orb")
    state.record_loss("A_orb", now)
    # Only 1 loss after win → no cooloff
    assert not state.arm_in_cooloff("A_orb", now)
