"""Unit tests for FreqAI-inspired feature pipeline."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.integrations.freqai_inspired.feature_pipeline import (
    FeatureSet,
    MIN_ROWS,
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


def _make_valid_ohlcv(rows: int = MIN_ROWS + 5) -> pd.DataFrame:
    """Helper: generate a valid OHLCV DataFrame with enough rows."""
    rng = np.random.default_rng(42)
    prices = 100 + np.cumsum(rng.normal(0, 1, rows))
    return pd.DataFrame(
        {
            "open": prices * 0.999,
            "high": prices * 1.002,
            "low": prices * 0.997,
            "close": prices,
            "volume": rng.integers(500_000, 2_000_000, rows).astype(float),
        }
    )


# ── Happy-path tests ──────────────────────────────────────────────────────────

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


# ── Fail-safe / input validation tests ───────────────────────────────────────

def test_nan_vix_returns_high_vol():
    """NaN VIX must block trading (fail-safe)."""
    fs = FeatureSet(
        rsi_14=65.0, atr_14_pct=1.5, vwap_deviation_pct=0.5,
        volume_ratio=1.2, india_vix=float("nan"),
        oi_pcr=0.9, bb_position=0.7, macd_signal_gap=0.5,
    )
    assert classify_regime(fs) == "high_vol", "NaN VIX must return high_vol"


def test_nan_rsi_returns_high_vol():
    """NaN RSI must block trading (fail-safe)."""
    fs = FeatureSet(
        rsi_14=float("nan"), atr_14_pct=1.5, vwap_deviation_pct=0.5,
        volume_ratio=1.2, india_vix=14.0,
        oi_pcr=0.9, bb_position=0.7, macd_signal_gap=0.5,
    )
    assert classify_regime(fs) == "high_vol"


def test_nan_macd_returns_high_vol():
    """NaN MACD must block trading (fail-safe)."""
    fs = FeatureSet(
        rsi_14=65.0, atr_14_pct=1.5, vwap_deviation_pct=0.5,
        volume_ratio=1.2, india_vix=14.0,
        oi_pcr=0.9, bb_position=0.7, macd_signal_gap=float("nan"),
    )
    assert classify_regime(fs) == "high_vol"


def test_too_few_rows_raises():
    """DataFrame with fewer than MIN_ROWS rows must raise ValueError."""
    small_df = pd.DataFrame(
        {c: [100.0] * (MIN_ROWS - 1) for c in ["open", "high", "low", "close", "volume"]}
    )
    with pytest.raises(ValueError, match="requires at least"):
        extract_features(small_df, 14.0, 1.0)


def test_missing_column_raises():
    """DataFrame missing required columns must raise ValueError."""
    bad_df = pd.DataFrame({"close": [100.0] * MIN_ROWS, "open": [99.0] * MIN_ROWS})
    with pytest.raises(ValueError, match="missing required columns"):
        extract_features(bad_df, 14.0, 1.0)


def test_nan_values_in_ohlcv_raises():
    """DataFrame with NaN OHLCV values must raise ValueError."""
    df = _make_valid_ohlcv()
    df = df.copy()
    df.loc[5, "close"] = float("nan")
    with pytest.raises(ValueError, match="NaN values"):
        extract_features(df, 14.0, 1.0)


def test_valid_data_returns_feature_set():
    """Happy path: valid data returns a FeatureSet with no NaN."""
    df = _make_valid_ohlcv()
    fs = extract_features(df, 14.0, 1.0)
    assert not math.isnan(fs.rsi_14), "RSI should not be NaN with valid data"
    assert not math.isnan(fs.india_vix)
    assert fs.india_vix == 14.0


def test_classify_regime_bullish():
    fs = FeatureSet(
        rsi_14=65.0, atr_14_pct=1.0, vwap_deviation_pct=0.3,
        volume_ratio=1.5, india_vix=13.0,
        oi_pcr=0.85, bb_position=0.75, macd_signal_gap=0.8,
    )
    assert classify_regime(fs) == "trending_bullish"


def test_classify_regime_bearish():
    fs = FeatureSet(
        rsi_14=35.0, atr_14_pct=1.0, vwap_deviation_pct=-0.3,
        volume_ratio=1.5, india_vix=16.0,
        oi_pcr=1.4, bb_position=0.2, macd_signal_gap=-0.8,
    )
    assert classify_regime(fs) == "trending_bearish"


def test_classify_regime_high_vol_from_vix():
    fs = FeatureSet(
        rsi_14=50.0, atr_14_pct=2.5, vwap_deviation_pct=0.0,
        volume_ratio=2.0, india_vix=25.0,
        oi_pcr=1.1, bb_position=0.5, macd_signal_gap=0.1,
    )
    assert classify_regime(fs) == "high_vol"
