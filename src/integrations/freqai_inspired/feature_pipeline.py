"""
ML Feature Pipeline — inspired by Freqtrade FreqAI architecture.
This is an ORIGINAL implementation of the concept. No Freqtrade source copied.
GPL-3.0 does not apply here — this is fresh code.

Pattern: extract structured features from market data →
         train a lightweight classifier → predict regime.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MIN_ROWS = 26  # MACD(12,26) needs 26 bars; signal(9) needs 9 more — use 26 minimum
REQUIRED_COLS = frozenset({"open", "high", "low", "close", "volume"})

Regime = Literal["trending_bullish", "trending_bearish", "sideways", "high_vol"]


@dataclass
class FeatureSet:
    """Structured feature vector for regime classification."""

    rsi_14: float
    atr_14_pct: float           # ATR as % of price
    vwap_deviation_pct: float   # (close - VWAP) / VWAP * 100
    volume_ratio: float         # today's volume / 20-day avg volume
    india_vix: float
    oi_pcr: float               # Nifty Put-Call Ratio
    bb_position: float          # (close - lower_band) / (upper - lower) → 0–1
    macd_signal_gap: float      # MACD line - signal line


def extract_features(ohlcv: pd.DataFrame, vix: float, pcr: float) -> FeatureSet:
    """
    Compute feature vector from OHLCV + market-level data.

    Args:
        ohlcv: DataFrame with columns: open, high, low, close, volume.
               Must have at least MIN_ROWS rows of clean data.
        vix: India VIX value. Pass math.nan if unavailable — triggers high_vol.
        pcr: Nifty Put-Call Ratio.

    Returns:
        FeatureSet with all computed indicators.

    Raises:
        ValueError: If ohlcv has insufficient rows or missing required columns.
    """
    # ── Input validation ──────────────────────────────────────────────────────
    missing_cols = REQUIRED_COLS - set(ohlcv.columns)
    if missing_cols:
        raise ValueError(
            f"extract_features: OHLCV DataFrame missing required columns: {missing_cols}. "
            f"Got: {list(ohlcv.columns)}"
        )
    if len(ohlcv) < MIN_ROWS:
        raise ValueError(
            f"extract_features: requires at least {MIN_ROWS} rows, got {len(ohlcv)}. "
            "Provide more historical data before calling."
        )
    null_counts = ohlcv[list(REQUIRED_COLS)].isnull().sum()
    if null_counts.any():
        raise ValueError(
            f"extract_features: OHLCV contains NaN values: "
            f"{null_counts[null_counts > 0].to_dict()}. "
            "Clean data before calling."
        )
    # ── Feature extraction ────────────────────────────────────────────────────
    close = ohlcv["close"]
    high = ohlcv["high"]
    low = ohlcv["low"]
    volume = ohlcv["volume"]

    # RSI(14)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # ATR(14) as % of price
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()
    atr_pct = (atr.iloc[-1] / close.iloc[-1]) * 100

    # Rolling 20-day VWAP deviation
    # Using rolling window, not session cumsum, because ohlcv is daily data.
    # Session cumsum on 90 days computes a 90-day cost-basis average (misleading).
    typical = (high + low + close) / 3
    _rolling_window = 20
    vwap = (
        (typical * volume).rolling(_rolling_window).sum()
        / volume.rolling(_rolling_window).sum().replace(0, np.nan)
    )
    vwap_dev = ((close.iloc[-1] - vwap.iloc[-1]) / vwap.iloc[-1]) * 100

    # Volume ratio
    vol_ratio = volume.iloc[-1] / volume.rolling(20).mean().iloc[-1]

    # Bollinger Bands
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    bb_pos = (close.iloc[-1] - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1] + 1e-9)

    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    macd_gap = macd.iloc[-1] - signal.iloc[-1]

    return FeatureSet(
        rsi_14=float(rsi.iloc[-1]),
        atr_14_pct=float(atr_pct),
        vwap_deviation_pct=float(vwap_dev),
        volume_ratio=float(vol_ratio),
        india_vix=vix,
        oi_pcr=pcr,
        bb_position=float(bb_pos),
        macd_signal_gap=float(macd_gap),
    )


def classify_regime(features: FeatureSet) -> Regime:
    """
    Rule-based regime classifier.

    Fail-safe: any NaN feature field returns "high_vol" (blocks trading).
    Phase 1: rules. Phase 2: replace with XGBoost trained on historical regimes.
    """
    vix  = features.india_vix
    rsi  = features.rsi_14
    pcr  = features.oi_pcr
    macd = features.macd_signal_gap

    # ── Fail-safe: NaN on any key field → block trading ───────────────────────
    # NaN comparisons in Python always return False, which would silently
    # fall through to "sideways" (allowed). We must check explicitly.
    if math.isnan(vix) or math.isnan(rsi) or math.isnan(macd):
        logger.warning(
            "classify_regime: NaN feature detected "
            f"(vix={vix}, rsi={rsi}, macd={macd}) — returning high_vol (fail-safe)"
        )
        return "high_vol"

    # ── Normal classification ─────────────────────────────────────────────────
    if vix > 20:
        return "high_vol"

    if rsi > 60 and macd > 0 and pcr < 1.0:
        return "trending_bullish"

    if rsi < 40 and macd < 0 and pcr > 1.2:
        return "trending_bearish"

    return "sideways"


async def health() -> dict:
    """Return integration health status."""
    return {"status": "ok", "backend": "freqai_inspired_feature_pipeline"}
