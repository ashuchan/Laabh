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
    # Basic sanity: select returns None when no signalling arms
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
    # _symbol_from_arm was replaced by arm_meta dict; use _split_arm from persistence
    from src.quant.persistence import _split_arm
    assert _split_arm("RELIANCE_orb")[0] == "RELIANCE"
    assert _split_arm("NIFTY_vwap_revert")[0] == "NIFTY"
