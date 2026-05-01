"""Unit tests for ShortStraddle strategy — trailing stop logic."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.laabh.strategies.short_straddle import ShortStraddle, ShortStraddleConfig


@pytest.fixture
def cfg() -> ShortStraddleConfig:
    return ShortStraddleConfig(
        underlying="BANKNIFTY",
        expiry="27JUN25",
        atm_strike=50000,
        lot_size=15,
        trailing_stop_pct=0.25,
        daily_target_pct=0.30,
        discipline_mode=True,
    )


def test_no_trailing_stop_before_peak(cfg):
    straddle = ShortStraddle(cfg)
    # peak_pnl starts at 0 — no stop should fire on positive P&L
    assert straddle.update_trailing_stop(500.0) is None
    assert straddle.peak_pnl == 500.0


def test_trailing_stop_triggered_after_pullback(cfg):
    straddle = ShortStraddle(cfg)
    straddle.update_trailing_stop(1000.0)   # peak set to 1000
    # trailing stop level = 1000 * (1 - 0.25) = 750
    assert straddle.update_trailing_stop(700.0) == "TRAILING_STOP"
    assert straddle.stop_triggered is True


def test_trailing_stop_not_triggered_within_band(cfg):
    straddle = ShortStraddle(cfg)
    straddle.update_trailing_stop(1000.0)
    # 800 > 750 — should not trigger
    assert straddle.update_trailing_stop(800.0) is None
    assert straddle.stop_triggered is False


def test_discipline_mode_sets_flag(cfg):
    straddle = ShortStraddle(cfg)
    straddle.update_trailing_stop(500.0)
    straddle.update_trailing_stop(100.0)  # below 500*0.75=375
    assert straddle.stop_triggered is True


def test_discipline_mode_off_does_not_set_flag():
    cfg = ShortStraddleConfig(
        underlying="NIFTY", expiry="27JUN25", atm_strike=24000,
        lot_size=50, discipline_mode=False
    )
    straddle = ShortStraddle(cfg)
    straddle.update_trailing_stop(1000.0)
    straddle.update_trailing_stop(100.0)
    assert straddle.stop_triggered is False


def test_enter_places_two_legs(cfg):
    with patch("src.laabh.strategies.short_straddle.place_paper_order") as mock_order:
        mock_order.return_value = {"status": "ok"}
        result = ShortStraddle(cfg).enter()

    assert result["status"] == "entered"
    assert len(result["legs"]) == 2
    assert mock_order.call_count == 2


def test_enter_leg_symbols(cfg):
    calls = []
    with patch("src.laabh.strategies.short_straddle.place_paper_order") as mock_order:
        mock_order.side_effect = lambda **kw: calls.append(kw) or {"status": "ok"}
        ShortStraddle(cfg).enter()

    symbols = {c["symbol"] for c in calls}
    assert "BANKNIFTY27JUN2550000CE" in symbols
    assert "BANKNIFTY27JUN2550000PE" in symbols
