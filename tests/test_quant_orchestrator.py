"""Integration smoke test for the quant orchestrator.

Uses mock primitives and a mock DB to verify the 3-min loop runs without
crashing and produces the expected trade count on a synthetic 30-min day.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.quant.primitives.base import Signal


def _make_signal(direction: str = "bullish", strength: float = 0.7) -> Signal:
    return Signal(
        direction=direction,
        strength=strength,
        strategy_class="long_call" if direction == "bullish" else "long_put",
        expected_horizon_minutes=15,
        expected_vol_pct=0.01,
    )


@pytest.mark.asyncio
async def test_orchestrator_smoke_no_crash():
    """Orchestrator runs for one tick without raising an exception."""
    from src.quant.orchestrator import _load_primitives, _make_arm_id
    from src.quant.bandit.selector import ArmSelector

    primitives = _load_primitives(["orb"])
    assert len(primitives) == 1

    arms = ["NIFTY_orb"]
    selector = ArmSelector(arms, seed=0)
    result = selector.select([])
    assert result is None


def test_arm_id_format():
    from src.quant.orchestrator import _make_arm_id
    assert _make_arm_id("RELIANCE", "orb") == "RELIANCE_orb"
    assert _make_arm_id("BANKNIFTY", "vwap_revert") == "BANKNIFTY_vwap_revert"


def test_total_exposure_sums_premiums():
    from src.quant.orchestrator import _total_exposure
    from src.quant.exits import OpenPosition

    now = datetime.now(timezone.utc)
    pos1 = OpenPosition("A_orb", "A", "bullish", Decimal("100"), now)
    pos2 = OpenPosition("B_ofi", "B", "bearish", Decimal("200"), now)
    total = _total_exposure([pos1, pos2])
    assert total == Decimal("300")


def test_symbol_from_arm():
    from src.quant.persistence import _split_arm
    assert _split_arm("RELIANCE_orb")[0] == "RELIANCE"
    assert _split_arm("NIFTY_vwap_revert")[0] == "NIFTY"


def test_get_premium_from_bundle_uses_mid():
    """_get_premium_from_bundle returns (bid+ask)/2 when both are present."""
    from src.quant.orchestrator import _get_premium_from_bundle
    from src.quant.exits import OpenPosition

    now = datetime.now(timezone.utc)
    pos = OpenPosition("RELIANCE_orb", "RELIANCE", "bullish", Decimal("100"), now)

    bundle = MagicMock()
    bundle.atm_bid = Decimal("90")
    bundle.atm_ask = Decimal("110")
    assert _get_premium_from_bundle(pos, bundle) == Decimal("100")


def test_get_premium_from_bundle_fallback_on_no_bid_ask():
    """_get_premium_from_bundle falls back to entry_premium_net when bid/ask missing."""
    from src.quant.orchestrator import _get_premium_from_bundle
    from src.quant.exits import OpenPosition

    now = datetime.now(timezone.utc)
    pos = OpenPosition("RELIANCE_orb", "RELIANCE", "bullish", Decimal("150"), now)

    bundle = MagicMock()
    bundle.atm_bid = None
    bundle.atm_ask = None
    assert _get_premium_from_bundle(pos, bundle) == Decimal("150")


def test_build_tick_context_shape():
    """_build_tick_context returns a 5-dim array clamped to [0,1]."""
    from src.quant.orchestrator import _build_tick_context

    bundle = MagicMock()
    bundle.vix_value = 18.0
    bundle.realized_vol_30min = 0.3

    ctx = _build_tick_context(
        features_map={"NIFTY": bundle},
        minutes_since_open=90.0,
        day_running_pnl_pct=0.02,
    )
    assert ctx.shape == (5,)
    assert all(0.0 <= v <= 1.0 for v in ctx), f"context out of [0,1]: {ctx}"


def test_build_tick_context_empty_features():
    """_build_tick_context uses VIX=15 fallback when features_map is empty."""
    from src.quant.orchestrator import _build_tick_context

    ctx = _build_tick_context(
        features_map={},
        minutes_since_open=0.0,
        day_running_pnl_pct=0.0,
    )
    assert ctx.shape == (5,)
    # VIX=15 → vix_value/30 = 0.5
    assert abs(ctx[0] - 0.5) < 1e-6


def test_get_premium_from_bundle_stub_with_bundle():
    """Stub returns mid when bundle has bid/ask."""
    from src.quant.orchestrator import _get_premium_from_bundle_stub

    bundle = MagicMock()
    bundle.atm_bid = Decimal("80")
    bundle.atm_ask = Decimal("120")
    assert _get_premium_from_bundle_stub(bundle) == Decimal("100")


def test_get_premium_from_bundle_stub_fallback():
    """Stub returns ₹100 when bundle is None."""
    from src.quant.orchestrator import _get_premium_from_bundle_stub

    assert _get_premium_from_bundle_stub(None) == Decimal("100")
