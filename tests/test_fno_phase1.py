"""Tests for F&O universe Phase 1 liquidity filter (pure helpers only)."""
from __future__ import annotations

import pytest

from src.fno.universe import (
    apply_liquidity_filter,
    check_atm_oi,
    check_spread,
    check_volume,
    compute_atm_spread_pct,
)

MIN_OI = 50_000
MAX_SPREAD = 0.005   # 0.5% as fraction
MIN_VOL = 10_000


# ---------------------------------------------------------------------------
# check_atm_oi
# ---------------------------------------------------------------------------

def test_check_atm_oi_passes() -> None:
    ok, reason = check_atm_oi(60_000, MIN_OI)
    assert ok is True
    assert reason is None


def test_check_atm_oi_fails_below() -> None:
    ok, reason = check_atm_oi(40_000, MIN_OI)
    assert ok is False
    assert reason is not None


def test_check_atm_oi_fails_none() -> None:
    ok, reason = check_atm_oi(None, MIN_OI)
    assert ok is False


def test_check_atm_oi_exact_boundary() -> None:
    ok, _ = check_atm_oi(50_000, MIN_OI)
    assert ok is True


# ---------------------------------------------------------------------------
# check_spread
# ---------------------------------------------------------------------------

def test_check_spread_passes() -> None:
    ok, reason = check_spread(0.003, MAX_SPREAD)
    assert ok is True
    assert reason is None


def test_check_spread_fails_wide() -> None:
    ok, reason = check_spread(0.010, MAX_SPREAD)
    assert ok is False
    assert reason is not None


def test_check_spread_fails_none() -> None:
    ok, reason = check_spread(None, MAX_SPREAD)
    assert ok is False


# ---------------------------------------------------------------------------
# check_volume
# ---------------------------------------------------------------------------

def test_check_volume_passes() -> None:
    ok, reason = check_volume(500_000, MIN_VOL)
    assert ok is True


def test_check_volume_fails_low() -> None:
    ok, reason = check_volume(5_000, MIN_VOL)
    assert ok is False


def test_check_volume_none_passes() -> None:
    # Missing volume is not a hard fail
    ok, reason = check_volume(None, MIN_VOL)
    assert ok is True
    assert reason is None


# ---------------------------------------------------------------------------
# apply_liquidity_filter
# ---------------------------------------------------------------------------

def test_apply_liquidity_filter_all_pass() -> None:
    ok, reason = apply_liquidity_filter(
        60_000, 0.003, 500_000,
        min_oi=MIN_OI, max_spread_pct=MAX_SPREAD, min_volume=MIN_VOL,
    )
    assert ok is True
    assert reason is None


def test_apply_liquidity_filter_oi_fails_first() -> None:
    ok, reason = apply_liquidity_filter(
        1_000, 0.003, 500_000,
        min_oi=MIN_OI, max_spread_pct=MAX_SPREAD, min_volume=MIN_VOL,
    )
    assert ok is False
    assert "atm_oi" in (reason or "")


def test_apply_liquidity_filter_spread_fails() -> None:
    ok, reason = apply_liquidity_filter(
        60_000, 0.020, 500_000,
        min_oi=MIN_OI, max_spread_pct=MAX_SPREAD, min_volume=MIN_VOL,
    )
    assert ok is False
    assert "spread" in (reason or "")


def test_apply_liquidity_filter_volume_fails() -> None:
    ok, reason = apply_liquidity_filter(
        60_000, 0.003, 100,
        min_oi=MIN_OI, max_spread_pct=MAX_SPREAD, min_volume=MIN_VOL,
    )
    assert ok is False
    assert "vol" in (reason or "")


def test_apply_liquidity_filter_no_volume_data_passes() -> None:
    ok, reason = apply_liquidity_filter(
        60_000, 0.003, None,
        min_oi=MIN_OI, max_spread_pct=MAX_SPREAD, min_volume=MIN_VOL,
    )
    assert ok is True


# ---------------------------------------------------------------------------
# compute_atm_spread_pct
# ---------------------------------------------------------------------------

def test_compute_atm_spread_pct_basic() -> None:
    # bid=99, ask=101, mid=100 → spread=2/100=0.02
    result = compute_atm_spread_pct(99.0, 101.0, 100.0)
    assert result is not None
    assert abs(result - 0.02) < 1e-6


def test_compute_atm_spread_pct_zero_mid_returns_none() -> None:
    assert compute_atm_spread_pct(0.0, 0.0, 0.0) is None


def test_compute_atm_spread_pct_none_bid_returns_none() -> None:
    assert compute_atm_spread_pct(None, 101.0, 100.0) is None
