"""Integration tests for NautilusTrader backtesting adapter."""
from __future__ import annotations

import pandas as pd
import pytest


@pytest.mark.integration
def test_run_signal_backtest():
    from src.integrations.nautilus.backtester import run_signal_backtest

    signal_history = [
        {
            "date": "2026-01-02",
            "ticker": "RELIANCE",
            "direction": "BUY",
            "entry": 2800.0,
            "target": 2900.0,
            "stop": 2750.0,
        }
    ]
    ohlcv = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-02", periods=20, freq="B"),
            "open": [2800 + i * 5 for i in range(20)],
            "high": [2820 + i * 5 for i in range(20)],
            "low": [2790 + i * 5 for i in range(20)],
            "close": [2810 + i * 5 for i in range(20)],
            "volume": [1_000_000] * 20,
        }
    )
    result = run_signal_backtest(signal_history, ohlcv, "RELIANCE")
    assert result["ticker"] == "RELIANCE"
    assert "total_return_pct" in result
    assert "sharpe_ratio" in result
    assert "win_rate" in result


async def test_nautilus_health():
    from src.integrations.nautilus.backtester import health

    result = await health()
    assert result["status"] in ("ok", "down")
    assert "backend" in result
