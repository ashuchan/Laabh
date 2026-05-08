"""Tests for vol-breakout primitive."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.vol_breakout import VolBreakoutPrimitive

UID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _bundle(ltp: float, bb_width: float = 0.02) -> FeatureBundle:
    return FeatureBundle(
        underlying_id=UID,
        underlying_symbol="HDFCBANK",
        captured_at=NOW,
        underlying_ltp=ltp,
        underlying_volume_3min=1000.0,
        vwap_today=1600.0,
        realized_vol_3min=0.005,
        realized_vol_30min=0.01,
        atm_iv=0.2,
        atm_oi=10000,
        atm_bid=Decimal("20"),
        atm_ask=Decimal("20.5"),
        bid_volume_3min_change=0,
        ask_volume_3min_change=0,
        bb_width=bb_width,
        vix_value=15.0,
        vix_regime="normal",
    )


def _history(n: int = 20, ltp: float = 1600.0, bb_width: float = 0.02) -> list[FeatureBundle]:
    return [_bundle(ltp, bb_width) for _ in range(n)]


def test_no_signal_during_warmup():
    prim = VolBreakoutPrimitive()
    assert prim.compute_signal(_bundle(1610.0, 0.04), []) is None


def test_bullish_expansion_above_sma():
    prim = VolBreakoutPrimitive()
    # bb_now = 0.04 > 1.5 × 0.02 (avg) → expansion; ltp 1610 > sma 1600
    hist = _history(20, ltp=1600.0, bb_width=0.02)
    sig = prim.compute_signal(_bundle(1610.0, 0.04), hist)
    assert sig is not None
    assert sig.direction == "bullish"


def test_bearish_expansion_below_sma():
    prim = VolBreakoutPrimitive()
    hist = _history(20, ltp=1600.0, bb_width=0.02)
    sig = prim.compute_signal(_bundle(1590.0, 0.04), hist)
    assert sig is not None
    assert sig.direction == "bearish"


def test_no_signal_without_expansion():
    prim = VolBreakoutPrimitive()
    hist = _history(20, bb_width=0.02)
    # bb_now = 0.025 < 1.5 × 0.02 = 0.03 → no breakout
    sig = prim.compute_signal(_bundle(1610.0, 0.025), hist)
    assert sig is None
