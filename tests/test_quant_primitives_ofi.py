"""Tests for OFI primitive."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.ofi import OFIPrimitive

UID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _bundle(bid_chg: float, ask_chg: float, atm_bid: float = 10.0, atm_ask: float = 10.5) -> FeatureBundle:
    return FeatureBundle(
        underlying_id=UID,
        underlying_symbol="NIFTY",
        captured_at=NOW,
        underlying_ltp=22000.0,
        underlying_volume_3min=1000.0,
        vwap_today=22000.0,
        realized_vol_3min=0.005,
        realized_vol_30min=0.01,
        atm_iv=0.15,
        atm_oi=50000,
        atm_bid=Decimal(str(atm_bid)),
        atm_ask=Decimal(str(atm_ask)),
        bid_volume_3min_change=bid_chg,
        ask_volume_3min_change=ask_chg,
        bb_width=0.02,
        vix_value=14.0,
        vix_regime="normal",
    )


def _history(n: int = 5) -> list[FeatureBundle]:
    return [_bundle(100.0, 100.0) for _ in range(n)]


def test_no_signal_during_warmup():
    prim = OFIPrimitive()
    assert prim.compute_signal(_bundle(500, 100), []) is None


def test_bullish_on_positive_ofi():
    prim = OFIPrimitive()
    hist = _history()
    sig = prim.compute_signal(_bundle(1000, 100), hist)
    assert sig is not None
    assert sig.direction == "bullish"


def test_bearish_on_negative_ofi():
    prim = OFIPrimitive()
    hist = _history()
    sig = prim.compute_signal(_bundle(100, 1000), hist)
    assert sig is not None
    assert sig.direction == "bearish"


def test_stale_quote_returns_none():
    prim = OFIPrimitive()
    hist = _history()
    sig = prim.compute_signal(_bundle(500, 100, atm_bid=0.0, atm_ask=0.0), hist)
    assert sig is None


def test_strength_within_bounds():
    prim = OFIPrimitive()
    hist = _history()
    sig = prim.compute_signal(_bundle(10000, 100), hist)
    assert sig is not None
    assert 0 <= sig.strength <= 1.0
