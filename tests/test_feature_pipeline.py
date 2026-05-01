"""Unit tests for FreqAI-inspired feature pipeline."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.integrations.freqai_inspired.feature_pipeline import (
    FeatureSet,
    classify_regime,
    extract_features,
)


def _make_ohlcv(n: int = 50, trend: float = 0.0) -> pd.DataFrame:
    """Build synthetic OHLCV DataFrame for testing."""
    base = 1000.0
    closes = [base + i * trend + np.random.uniform(-5, 5) for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 2 for c in closes],
            "high": [c + 5 for c in closes],
            "low": [c - 5 for c in closes],
            "close": closes,
            "volume": [500_000 + i * 1000 for i in range(n)],
        }
    )


def test_extract_features_returns_featureset():
    ohlcv = _make_ohlcv(50, trend=2.0)
    fs = extract_features(ohlcv, vix=15.0, pcr=0.9)
    assert isinstance(fs, FeatureSet)
    assert 0 <= fs.rsi_14 <= 100
    assert fs.india_vix == 15.0
    assert fs.oi_pcr == 0.9
    assert 0.0 <= fs.bb_position <= 1.5  # can exceed 1 in strong trends


def test_extract_features_volume_ratio():
    ohlcv = _make_ohlcv(50)
    fs = extract_features(ohlcv, vix=14.0, pcr=1.0)
    assert fs.volume_ratio > 0


def test_classify_regime_high_vol():
    fs = FeatureSet(
        rsi_14=55.0, atr_14_pct=1.5, vwap_deviation_pct=0.5,
        volume_ratio=1.2, india_vix=22.0, oi_pcr=1.0,
        bb_position=0.6, macd_signal_gap=5.0
    )
    assert classify_regime(fs) == "high_vol"


def test_classify_regime_trending_bullish():
    fs = FeatureSet(
        rsi_14=65.0, atr_14_pct=1.0, vwap_deviation_pct=1.2,
        volume_ratio=1.5, india_vix=13.0, oi_pcr=0.8,
        bb_position=0.8, macd_signal_gap=10.0
    )
    assert classify_regime(fs) == "trending_bullish"


def test_classify_regime_trending_bearish():
    fs = FeatureSet(
        rsi_14=35.0, atr_14_pct=1.0, vwap_deviation_pct=-1.5,
        volume_ratio=1.2, india_vix=18.0, oi_pcr=1.4,
        bb_position=0.2, macd_signal_gap=-8.0
    )
    assert classify_regime(fs) == "trending_bearish"


def test_classify_regime_sideways():
    fs = FeatureSet(
        rsi_14=50.0, atr_14_pct=0.8, vwap_deviation_pct=0.1,
        volume_ratio=0.9, india_vix=15.0, oi_pcr=1.0,
        bb_position=0.5, macd_signal_gap=0.5
    )
    assert classify_regime(fs) == "sideways"


async def test_feature_pipeline_health():
    from src.integrations.freqai_inspired.feature_pipeline import health

    result = await health()
    assert result["status"] == "ok"
