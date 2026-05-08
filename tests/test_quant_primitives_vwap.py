"""Tests for VWAP-revert primitive."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.vwap_revert import VWAPRevertPrimitive

UID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _bundle(ltp: float, vwap: float = 100.0, rv30: float = 1.0) -> FeatureBundle:
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
    # Alternating ±1 around 100 → price_std ≈ 1.05, ratio of ups/downs ≈ 0.5
    # so the trending gate stays open.
    return [_bundle(99.0 + (i % 2) * 2.0) for i in range(n)]


def test_no_signal_during_warmup():
    prim = VWAPRevertPrimitive()
    assert prim.compute_signal(_bundle(103.0), []) is None


def test_bearish_signal_when_z_exceeds_threshold():
    prim = VWAPRevertPrimitive()
    # price_std ≈ 1.05 → Z = (103 - 100) / 1.05 ≈ 2.86 > 2 → bearish
    hist = _flat_history()
    sig = prim.compute_signal(_bundle(103.0), hist)
    assert sig is not None
    assert sig.direction == "bearish"
    assert sig.strength > 0


def test_bullish_signal_when_z_below_neg_threshold():
    prim = VWAPRevertPrimitive()
    hist = _flat_history()
    # Z = (97 - 100) / 1.05 ≈ -2.86 → bullish
    sig = prim.compute_signal(_bundle(97.0), hist)
    assert sig is not None
    assert sig.direction == "bullish"


def test_no_signal_inside_band():
    prim = VWAPRevertPrimitive()
    hist = _flat_history()
    # Z = (101 - 100) / 1.05 ≈ 0.95 < 2 → no signal
    sig = prim.compute_signal(_bundle(101.0), hist)
    assert sig is None


def test_trending_suppresses_signal():
    prim = VWAPRevertPrimitive()
    # All bars trending up → is_trending = True → no signal
    hist = [_bundle(90.0 + i) for i in range(10)]
    sig = prim.compute_signal(_bundle(103.0), hist)
    assert sig is None
