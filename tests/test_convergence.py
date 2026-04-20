"""Tests for convergence scoring and technical indicators."""
from __future__ import annotations

import numpy as np
import pytest

from src.analytics.convergence import ConvergenceEngine


@pytest.fixture
def engine():
    return ConvergenceEngine()


def test_rsi_overbought(engine):
    # 14 days of monotonically rising prices → RSI should be high
    prices = np.linspace(100, 200, 30)
    rsi = engine._calc_rsi(prices)
    assert rsi > 70, f"Expected RSI > 70, got {rsi}"


def test_rsi_oversold(engine):
    # 14 days of monotonically falling prices → RSI should be low
    prices = np.linspace(200, 100, 30)
    rsi = engine._calc_rsi(prices)
    assert rsi < 30, f"Expected RSI < 30, got {rsi}"


def test_rsi_neutral(engine):
    # Alternating prices → RSI near 50
    prices = np.array([100, 110, 105, 115, 108, 118, 112, 122, 115, 125,
                       118, 128, 120, 130, 122, 132, 125, 135], dtype=float)
    rsi = engine._calc_rsi(prices)
    assert 30 <= rsi <= 70


def test_macd_bullish(engine):
    # Prices accelerating upward → MACD line > signal line
    t = np.linspace(0, 10, 50)
    prices = 100 + t ** 2  # accelerating
    macd, signal = engine._calc_macd(prices)
    assert macd > signal, f"Expected bullish MACD, got macd={macd} signal={signal}"


def test_ema_length(engine):
    data = np.arange(1, 31, dtype=float)
    ema = engine._ema(data, period=12)
    assert len(ema) == len(data)


def test_rsi_requires_data(engine):
    prices = np.array([100.0, 110.0, 105.0])
    rsi = engine._calc_rsi(prices, period=14)
    # Should not raise even with insufficient data
    assert 0 <= rsi <= 100
