"""
Replace hardcoded VIX threshold with feature-based regime classifier.
"""
from __future__ import annotations

import pandas as pd

from src.integrations.freqai_inspired.feature_pipeline import (
    Regime,
    classify_regime,
    extract_features,
)

ALLOWED_REGIMES: frozenset[Regime] = frozenset(
    {"trending_bullish", "trending_bearish", "sideways"}
)
BLOCKED_REGIMES: frozenset[Regime] = frozenset({"high_vol"})


async def is_regime_tradeable(
    ohlcv: pd.DataFrame,
    vix: float,
    pcr: float,
) -> tuple[bool, Regime]:
    """
    Returns (tradeable: bool, regime: Regime).
    Replaces the old `if vix > 20: block_all` hardcoded gate.

    Args:
        ohlcv: DataFrame with columns: open, high, low, close, volume
        vix: India VIX value
        pcr: Nifty Put-Call Ratio

    Returns:
        Tuple of (is_tradeable, detected_regime).
    """
    features = extract_features(ohlcv, vix, pcr)
    regime = classify_regime(features)
    return regime not in BLOCKED_REGIMES, regime
