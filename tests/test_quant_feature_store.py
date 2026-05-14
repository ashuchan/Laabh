"""Unit tests for src.quant.feature_store — synthetic data, no DB required."""
from __future__ import annotations

import math

import pytest

from src.quant.feature_store import (
    _bb_width,
    _compute_vwap,
    _realized_vol,
)


def test_vwap_uniform_volume():
    ltps = [100.0, 102.0, 98.0]
    vols = [1.0, 1.0, 1.0]
    assert _compute_vwap(ltps, vols) == pytest.approx(100.0, rel=1e-6)


def test_vwap_zero_volume_falls_back_to_arithmetic_mean():
    # Live mode never has underlying spot volume in options_chain, so every
    # vols vector arrives as zeros. Falling back to the *last* LTP would make
    # VWAP ≡ LTP and the vwap_revert primitive could never fire. Use the
    # arithmetic mean of the window instead so the primitive has a meaningful
    # anchor.
    ltps = [100.0, 102.0]
    vols = [0.0, 0.0]
    result = _compute_vwap(ltps, vols)
    assert result == pytest.approx(101.0)


def test_vwap_empty_returns_zero():
    assert _compute_vwap([], []) == 0.0


def test_realized_vol_flat():
    ltps = [100.0] * 10
    rv = _realized_vol(ltps)
    assert rv == pytest.approx(0.0, abs=1e-9)


def test_realized_vol_positive():
    import random
    rng = random.Random(42)
    ltps = [100.0]
    for _ in range(29):
        ltps.append(ltps[-1] * (1 + rng.gauss(0, 0.001)))
    rv = _realized_vol(ltps)
    assert rv > 0


def test_bb_width_single():
    assert _bb_width([100.0]) == pytest.approx(0.0)


def test_bb_width_constant():
    assert _bb_width([100.0] * 20) == pytest.approx(0.0, abs=1e-9)


def test_bb_width_positive():
    ltps = [100.0 + i for i in range(20)]
    bw = _bb_width(ltps)
    assert bw > 0


def test_realized_vol_matches_formula():
    # With a single log-return, mean equals that return, so sample variance = 0.
    ltps = [100.0, 101.0]
    rv = _realized_vol(ltps)
    assert rv == pytest.approx(0.0, abs=1e-9)

def test_realized_vol_two_returns():
    # With two log-returns, variance is non-zero.
    ltps = [100.0, 101.0, 100.0]
    rv = _realized_vol(ltps)
    assert rv > 0
