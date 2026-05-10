"""Tests for VWAP-revert primitive (Phase 2 z-math rewrite included).

Math reminder for these tests:
    expected_displacement = rv × √(30 / 94_500) × LTP
                          ≈ rv × 0.01781 × LTP

So with rv=0.30 and LTP=100 → σ_disp ≈ 0.534.
With rv=0.30 and LTP=33_575 → σ_disp ≈ 597.9.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.vwap_revert import VWAPRevertPrimitive

UID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _bundle(ltp: float, vwap: float = 100.0, rv30: float = 0.30) -> FeatureBundle:
    """Default rv30 = 0.30 (30% annualised) — typical Indian midcap intraday."""
    return FeatureBundle(
        underlying_id=UID,
        underlying_symbol="RELIANCE",
        captured_at=NOW,
        underlying_ltp=ltp,
        underlying_volume_3min=1000.0,
        vwap_today=vwap,
        realized_vol_3min=rv30 * 0.5,
        realized_vol_30min=rv30,
        atm_iv=0.2,
        atm_oi=10000,
        atm_bid=Decimal("10"),
        atm_ask=Decimal("10.5"),
        bid_volume_3min_change=0,
        ask_volume_3min_change=0,
        bb_width=0.02,
        vix_value=15.0,
        vix_regime="normal",
    )


def _flat_history(n: int = 10) -> list[FeatureBundle]:
    """Alternating ±1 around 100 → trending gate stays open (~50/50 ups)."""
    return [_bundle(99.0 + (i % 2) * 2.0) for i in range(n)]


# ---------------------------------------------------------------------------
# Original behaviours (warmup, direction, no-fire-in-band, trending gate)
# ---------------------------------------------------------------------------

def test_no_signal_during_warmup():
    prim = VWAPRevertPrimitive()
    assert prim.compute_signal(_bundle(103.0), []) is None


def test_bearish_signal_when_z_exceeds_threshold():
    prim = VWAPRevertPrimitive()
    # σ_disp ≈ 0.30 × 0.01781 × 102 ≈ 0.545; z = (102-100)/0.545 ≈ 3.67 > 2 → bearish
    sig = prim.compute_signal(_bundle(ltp=102.0, vwap=100.0), _flat_history())
    assert sig is not None
    assert sig.direction == "bearish"
    assert sig.strength > 0


def test_bullish_signal_when_z_below_neg_threshold():
    prim = VWAPRevertPrimitive()
    # z ≈ -3.74 → bullish
    sig = prim.compute_signal(_bundle(ltp=98.0, vwap=100.0), _flat_history())
    assert sig is not None
    assert sig.direction == "bullish"


def test_no_signal_inside_band():
    prim = VWAPRevertPrimitive()
    # σ_disp ≈ 0.534; z = (100.5-100)/0.534 ≈ 0.94 → below threshold → no signal
    sig = prim.compute_signal(_bundle(ltp=100.5, vwap=100.0), _flat_history())
    assert sig is None


def test_trending_suppresses_signal():
    prim = VWAPRevertPrimitive()
    hist = [_bundle(90.0 + i) for i in range(10)]  # all trending up
    sig = prim.compute_signal(_bundle(103.0), hist)
    assert sig is None


# ---------------------------------------------------------------------------
# New invariants — Phase 2 z-math rewrite
# ---------------------------------------------------------------------------

def test_returns_none_when_realized_vol_is_zero():
    """Without a vol estimate the displacement denominator collapses;
    we refuse to make a signal rather than dividing by ~0."""
    prim = VWAPRevertPrimitive()
    sig = prim.compute_signal(_bundle(ltp=103.0, vwap=100.0, rv30=0.0), _flat_history())
    assert sig is None


def test_z_is_capped_to_prevent_tail_outliers():
    """The pre-Phase-2 bug let |z| reach 31.9 on real data. After the cap,
    even an absurd displacement should leave |z| ≤ _Z_CAP (4.0)."""
    prim = VWAPRevertPrimitive()
    trace: dict = {}
    # Reproduce the POWERINDIA-style outlier: LTP 33,575, VWAP 34,027.
    # σ_disp ≈ 0.30 × 0.01781 × 33575 ≈ 179.4. raw_z ≈ -2.52 actually,
    # so to force the cap we use a deliberately tiny rv.
    sig = prim.compute_signal(
        _bundle(ltp=33575.0, vwap=34027.0, rv30=0.05),  # very low rv → huge raw_z
        _flat_history(),
        trace=trace,
    )
    assert sig is not None
    assert sig.direction == "bullish"  # ltp < vwap
    # Verify trace shows raw_z exceeded the cap and z_capped is bounded
    assert abs(trace["intermediates"]["raw_z"]) > 4.0
    assert abs(trace["intermediates"]["z_capped"]) <= 4.0


def test_z_capped_bounds_strength_below_one():
    """Capping z at 4 should make strength = tanh(4-2) = tanh(2) ≈ 0.964 max,
    not 1.0. This is the desired property — keeps the bandit able to
    discriminate "very strong" from "extreme outlier"."""
    prim = VWAPRevertPrimitive()
    sig = prim.compute_signal(
        _bundle(ltp=33575.0, vwap=34027.0, rv30=0.05),  # forces the cap
        _flat_history(),
    )
    assert sig is not None
    # tanh(2) ≈ 0.9640
    assert sig.strength <= math.tanh(2) + 1e-9


def test_real_world_powerindia_no_longer_explodes():
    """Reproduction of the smoke-data outlier: POWERINDIA at 33,575 with
    VWAP 34,027 used to report z = -31.9. With realistic rv30=0.30, the
    new math should produce |z| ≈ 2.5 — meaningful but not nonsense."""
    prim = VWAPRevertPrimitive()
    trace: dict = {}
    sig = prim.compute_signal(
        _bundle(ltp=33575.0, vwap=34027.0, rv30=0.30),
        _flat_history(),
        trace=trace,
    )
    assert sig is not None
    raw_z = trace["intermediates"]["raw_z"]
    # Sanity-check: |raw_z| should be in the single-digit range, not 30+
    assert 1.5 <= abs(raw_z) <= 5.0, f"raw_z={raw_z} — Bug 2 regression"


# ---------------------------------------------------------------------------
# Phase 3 — should_take_profit hook
# ---------------------------------------------------------------------------

def _open_position():
    """Lightweight stand-in for OpenPosition. ``should_take_profit`` only
    inspects ``current_features``, not the position itself, so a sentinel
    object is enough."""
    return object()


def test_take_profit_fires_when_z_drops_below_take_profit_threshold():
    prim = VWAPRevertPrimitive()
    # σ_disp ≈ 0.534; z = (100.4 - 100) / 0.534 ≈ 0.75 < 1.0 → take profit
    assert prim.should_take_profit(_open_position(), _bundle(ltp=100.4, vwap=100.0)) is True


def test_take_profit_does_not_fire_while_still_displaced():
    prim = VWAPRevertPrimitive()
    # z = (100.7 - 100) / 0.534 ≈ 1.31 → above threshold, hold the position
    assert prim.should_take_profit(_open_position(), _bundle(ltp=100.7, vwap=100.0)) is False


def test_take_profit_does_not_fire_at_entry_z():
    prim = VWAPRevertPrimitive()
    # z = (102 - 100) / 0.534 ≈ 3.74 — same point we'd ENTER at; obviously
    # not a take-profit moment. Defensive: ensure entry and exit don't
    # accidentally agree at the entry threshold.
    assert prim.should_take_profit(_open_position(), _bundle(ltp=102.0, vwap=100.0)) is False


def test_take_profit_handles_zero_vol_gracefully():
    prim = VWAPRevertPrimitive()
    # rv=0 → expected_displacement=0 → can't compute z → return False (don't crash)
    assert prim.should_take_profit(_open_position(), _bundle(ltp=100.2, vwap=100.0, rv30=0.0)) is False


def test_base_primitive_default_take_profit_is_false():
    """Sanity-check the contract: every primitive that doesn't override
    the hook gets the conservative default. Caught by importing any other
    primitive that doesn't override (e.g. orb)."""
    from src.quant.primitives.orb import ORBPrimitive
    assert ORBPrimitive().should_take_profit(_open_position(), _bundle(ltp=100.0)) is False


def test_take_profit_uses_same_z_math_as_compute_signal():
    """Property: a tick where compute_signal would fire (|z| > 2) must NOT
    trigger take_profit (|z| > 0.5). The two decisions share ``_compute_capped_z``
    so they can't drift apart."""
    prim = VWAPRevertPrimitive()
    # |z| ≈ 3.74 — above entry threshold
    bundle = _bundle(ltp=102.0, vwap=100.0)
    sig = prim.compute_signal(bundle, _flat_history())
    assert sig is not None  # entry would fire
    assert prim.should_take_profit(_open_position(), bundle) is False  # exit would NOT


def test_trace_includes_new_intermediates():
    """The trace shape changed — Decision Inspector reads these keys."""
    prim = VWAPRevertPrimitive()
    trace: dict = {}
    sig = prim.compute_signal(
        _bundle(ltp=102.0, vwap=100.0, rv30=0.30), _flat_history(), trace=trace,
    )
    assert sig is not None
    expected_keys = {"expected_displacement", "raw_z", "z_capped", "z_threshold", "z_cap"}
    assert expected_keys <= trace["intermediates"].keys()
    # Old keys (price_std, price_mean) must NOT appear — UI consumers updated
    assert "price_std" not in trace["intermediates"]
    assert "price_mean" not in trace["intermediates"]
