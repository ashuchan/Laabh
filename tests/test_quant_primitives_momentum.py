"""Tests for momentum primitive."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.momentum import MomentumPrimitive

UID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _bundle(ltp: float, vol: float = 1000.0, rv30: float = 0.01) -> FeatureBundle:
    return FeatureBundle(
        underlying_id=UID,
        underlying_symbol="RELIANCE",
        captured_at=NOW,
        underlying_ltp=ltp,
        underlying_volume_3min=vol,
        vwap_today=100.0,
        realized_vol_3min=0.005,
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


def test_no_signal_during_warmup():
    prim = MomentumPrimitive()
    assert prim.compute_signal(_bundle(101.0), []) is None


def test_bullish_on_rising_bars():
    prim = MomentumPrimitive()
    # Monotonically rising prices → positive momentum
    hist = [_bundle(100.0 + i * 0.1) for i in range(11)]
    sig = prim.compute_signal(_bundle(101.1), hist)
    assert sig is not None
    assert sig.direction == "bullish"
    assert sig.strength > 0


def test_bearish_on_falling_bars():
    prim = MomentumPrimitive()
    hist = [_bundle(100.0 - i * 0.1) for i in range(11)]
    sig = prim.compute_signal(_bundle(98.9), hist)
    assert sig is not None
    assert sig.direction == "bearish"


def test_strength_within_bounds():
    prim = MomentumPrimitive()
    hist = [_bundle(100.0 + i) for i in range(11)]
    sig = prim.compute_signal(_bundle(111.0), hist)
    assert sig is not None
    assert 0 <= sig.strength <= 1.0


def test_deterministic():
    prim = MomentumPrimitive()
    hist = [_bundle(100.0 + i * 0.1) for i in range(11)]
    b = _bundle(101.1)
    s1 = prim.compute_signal(b, hist)
    s2 = prim.compute_signal(b, hist)
    assert s1 is not None and s2 is not None
    assert s1.strength == s2.strength
