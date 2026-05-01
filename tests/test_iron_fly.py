"""Unit tests for IronFly strategy — MTM exit logic."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.laabh.strategies.iron_fly import IronFly, IronFlyConfig


@pytest.fixture
def cfg() -> IronFlyConfig:
    return IronFlyConfig(
        underlying="NIFTY",
        expiry="26JUN25",
        atm_strike=24000,
        lot_size=50,
        target_pct=0.40,
        stop_pct=1.00,
    )


def test_check_mtm_no_exit_within_range(cfg):
    fly = IronFly(cfg)
    fly.entry_premium = 200.0
    # P&L well within target and stop
    assert fly.check_mtm_exit(1000.0) is None


def test_check_mtm_target_hit(cfg):
    fly = IronFly(cfg)
    fly.entry_premium = 200.0
    # max_profit = 200 * 50 = 10_000; target = 10_000 * 0.40 = 4_000
    assert fly.check_mtm_exit(4000.0) == "TARGET"
    assert fly.check_mtm_exit(5000.0) == "TARGET"


def test_check_mtm_stop_hit(cfg):
    fly = IronFly(cfg)
    fly.entry_premium = 200.0
    # stop = -10_000 * 1.00 = -10_000
    assert fly.check_mtm_exit(-10000.0) == "STOP"
    assert fly.check_mtm_exit(-15000.0) == "STOP"


def test_check_mtm_boundary_not_triggered(cfg):
    fly = IronFly(cfg)
    fly.entry_premium = 200.0
    # Exactly at target boundary — should trigger
    assert fly.check_mtm_exit(4000.0) == "TARGET"
    # Just below stop — should not trigger
    assert fly.check_mtm_exit(-9999.0) is None


def test_enter_calls_place_paper_order(cfg):
    with patch("src.laabh.strategies.iron_fly.place_paper_order") as mock_order:
        mock_order.return_value = {"status": "ok"}
        fly = IronFly(cfg)
        result = fly.enter()

    assert result["status"] == "entered"
    assert len(result["legs"]) == 4
    assert mock_order.call_count == 4


def test_enter_places_correct_legs(cfg):
    """Verify the 4 legs use correct strikes and actions."""
    calls = []
    with patch("src.laabh.strategies.iron_fly.place_paper_order") as mock_order:
        mock_order.side_effect = lambda **kw: calls.append(kw) or {"status": "ok"}
        IronFly(cfg).enter()

    symbols = [c["symbol"] for c in calls]
    actions = [c["action"] for c in calls]

    assert "NIFTY26JUN2524000CE" in symbols
    assert "NIFTY26JUN2524000PE" in symbols
    assert "NIFTY26JUN2524100CE" in symbols  # ATM + 100 wing
    assert "NIFTY26JUN2523900PE" in symbols  # ATM - 100 wing
    assert actions.count("SELL") == 2
    assert actions.count("BUY") == 2
