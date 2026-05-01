"""
ML Feature Pipeline — inspired by Freqtrade FreqAI architecture.
This is an ORIGINAL implementation of the concept. No Freqtrade source copied.
GPL-3.0 does not apply here — this is fresh code.

Pattern: extract structured features from market data →
         train a lightweight classifier → predict regime.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

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
        ohlcv: DataFrame with columns: open, high, low, close, volume
        vix: India VIX value
        pcr: Nifty Put-Call Ratio

    Returns:
        FeatureSet with all computed indicators.
    """
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

    # VWAP deviation
    typical = (high + low + close) / 3
    vwap = (typical * volume).cumsum() / volume.cumsum()
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
    Phase 1: rules. Phase 2: replace with XGBoost trained on historical regimes.
    """
    vix = features.india_vix
    rsi = features.rsi_14
    pcr = features.oi_pcr
    macd = features.macd_signal_gap

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
