"""Tests for ORB primitive."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.orb import ORBPrimitive

UID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _bundle(ltp: float, vol: float = 1000.0, orb_high: float = 105.0, orb_low: float = 95.0) -> FeatureBundle:
    return FeatureBundle(
        underlying_id=UID,
        underlying_symbol="RELIANCE",
        captured_at=NOW,
        underlying_ltp=ltp,
        underlying_volume_3min=vol,
        vwap_today=100.0,
        realized_vol_3min=0.01,
        realized_vol_30min=0.015,
        atm_iv=0.2,
        atm_oi=10000,
        atm_bid=Decimal("10"),
        atm_ask=Decimal("10.5"),
        bid_volume_3min_change=0,
        ask_volume_3min_change=0,
        bb_width=0.02,
        vix_value=15.0,
        vix_regime="normal",
        orb_high=orb_high,
        orb_low=orb_low,
    )


def _history(n: int = 10) -> list[FeatureBundle]:
    return [_bundle(100.0) for _ in range(n)]


def test_no_signal_during_warmup():
    prim = ORBPrimitive()
    assert prim.compute_signal(_bundle(110.0), []) is None
    assert prim.compute_signal(_bundle(110.0), _history(5)) is None


def test_bullish_breakout():
    prim = ORBPrimitive()
    # LTP 108 > orb_high 105; volume well above avg
    hist = _history(10)
    sig = prim.compute_signal(_bundle(108.0, vol=2000.0), hist)
    assert sig is not None
    assert sig.direction == "bullish"
    assert 0 < sig.strength <= 1.0
    assert sig.strategy_class == "long_call"


def test_bearish_breakdown():
    prim = ORBPrimitive()
    hist = _history(10)
    sig = prim.compute_signal(_bundle(92.0, vol=2000.0), hist)
    assert sig is not None
    assert sig.direction == "bearish"
    assert sig.strategy_class == "long_put"


def test_no_signal_inside_range():
    prim = ORBPrimitive()
    hist = _history(10)
    sig = prim.compute_signal(_bundle(100.0, vol=2000.0), hist)
    assert sig is None


def test_no_signal_low_volume():
    prim = ORBPrimitive()
    # Avg volume in history = 1000; current = 500 < 1.5 × 1000
    hist = _history(10)
    sig = prim.compute_signal(_bundle(108.0, vol=500.0), hist)
    assert sig is None


def test_strength_clamped():
    prim = ORBPrimitive()
    # Extreme breakout
    hist = _history(10)
    sig = prim.compute_signal(_bundle(200.0, vol=5000.0), hist)
    assert sig is not None
    assert sig.strength <= 1.0


def test_deterministic():
    prim = ORBPrimitive()
    hist = _history(10)
    b = _bundle(108.0, vol=2000.0)
    s1 = prim.compute_signal(b, hist)
    s2 = prim.compute_signal(b, hist)
    assert s1 is not None and s2 is not None
    assert s1.strength == s2.strength
