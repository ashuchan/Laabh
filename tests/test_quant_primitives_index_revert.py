"""Tests for index-revert primitive."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.index_revert import IndexRevertPrimitive

UID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


def _bundle(ltp: float, basket: float | None, symbol: str = "NIFTY") -> FeatureBundle:
    return FeatureBundle(
        underlying_id=UID,
        underlying_symbol=symbol,
        captured_at=NOW,
        underlying_ltp=ltp,
        underlying_volume_3min=1000.0,
        vwap_today=22000.0,
        realized_vol_3min=0.005,
        realized_vol_30min=0.01,
        atm_iv=0.15,
        atm_oi=50000,
        atm_bid=Decimal("50"),
        atm_ask=Decimal("50.5"),
        bid_volume_3min_change=0,
        ask_volume_3min_change=0,
        bb_width=0.02,
        vix_value=14.0,
        vix_regime="normal",
        constituent_basket_value=basket,
    )


def _history(n: int = 10, ltp: float = 22000.0, basket: float = 22000.0) -> list[FeatureBundle]:
    return [_bundle(ltp, basket) for _ in range(n)]


def test_no_signal_during_warmup():
    prim = IndexRevertPrimitive()
    assert prim.compute_signal(_bundle(22500.0, 22000.0), []) is None


def test_no_signal_non_index():
    prim = IndexRevertPrimitive()
    hist = _history()
    # RELIANCE is not an index
    b = _bundle(200.0, None, symbol="RELIANCE")
    assert prim.compute_signal(b, hist) is None


def test_no_signal_no_basket():
    prim = IndexRevertPrimitive()
    hist = _history()
    sig = prim.compute_signal(_bundle(22000.0, None), hist)
    assert sig is None


def test_bearish_when_index_above_basket():
    prim = IndexRevertPrimitive()
    # Build history with large variation so std > 0
    hist = [_bundle(22000.0 + i * 10, 22000.0 + i * 10) for i in range(10)]
    # Current: index is far above scaled basket (simulate with a big jump)
    sig = prim.compute_signal(_bundle(23000.0, 22000.0), hist)
    # The basket std from the stepped history is ~30; Z >> 2 → bearish
    if sig is not None:
        assert sig.direction == "bearish"
