"""Unit tests for regime gate."""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from src.laabh.regime_gate import ALLOWED_REGIMES, BLOCKED_REGIMES, is_regime_tradeable


def _make_ohlcv(n: int = 50, trend: float = 0.0) -> pd.DataFrame:
    closes = [1000.0 + i * trend + np.random.uniform(-3, 3) for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 1 for c in closes],
            "high": [c + 4 for c in closes],
            "low": [c - 4 for c in closes],
            "close": closes,
            "volume": [600_000] * n,
        }
    )


async def test_high_vix_is_blocked():
    ohlcv = _make_ohlcv(50, trend=1.0)
    tradeable, regime = await is_regime_tradeable(ohlcv, vix=25.0, pcr=1.1)
    assert regime == "high_vol"
    assert tradeable is False
    assert regime in BLOCKED_REGIMES


async def test_normal_vix_is_tradeable():
    ohlcv = _make_ohlcv(50, trend=0.0)
    tradeable, regime = await is_regime_tradeable(ohlcv, vix=14.0, pcr=1.0)
    assert tradeable is True
    assert regime in ALLOWED_REGIMES


def test_blocked_and_allowed_sets_are_disjoint():
    assert ALLOWED_REGIMES.isdisjoint(BLOCKED_REGIMES)


def test_all_regimes_classified():
    """Every possible Regime is in either ALLOWED or BLOCKED."""
    all_regimes = {"trending_bullish", "trending_bearish", "sideways", "high_vol"}
    assert all_regimes == ALLOWED_REGIMES | BLOCKED_REGIMES


# ── Fail-safe tests ───────────────────────────────────────────────────────────

def test_empty_dataframe_blocks_trading():
    """Empty DataFrame must block trading — never raise."""
    result = asyncio.get_event_loop().run_until_complete(
        is_regime_tradeable(pd.DataFrame(), float("nan"), 1.0)
    )
    assert result == (False, "high_vol"), f"Expected fail-safe, got {result}"


def test_nan_vix_blocks_trading():
    """NaN VIX must block trading even with otherwise valid data."""
    df = pd.DataFrame(
        {c: [float(100 + i) for i in range(35)] for c in ["open", "high", "low", "close"]}
        | {"volume": [1_000_000.0] * 35}
    )
    result = asyncio.get_event_loop().run_until_complete(
        is_regime_tradeable(df, float("nan"), 1.0)
    )
    assert result[0] is False, f"NaN VIX should block trade, got {result}"
    assert result[1] == "high_vol"


def test_high_vix_blocks_trading():
    """VIX > 20 must block trading."""
    df = pd.DataFrame(
        {c: [float(100 + i) for i in range(35)] for c in ["open", "high", "low", "close"]}
        | {"volume": [1_000_000.0] * 35}
    )
    result = asyncio.get_event_loop().run_until_complete(
        is_regime_tradeable(df, 25.0, 1.0)
    )
    assert result[0] is False
    assert result[1] == "high_vol"
