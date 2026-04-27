"""Tests for fill simulator and position sizer."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.fno.execution.fill_simulator import (
    FillResult,
    simulate_fill,
    total_net_cost,
)
from src.fno.execution.sizer import (
    compute_lots,
    compute_stop_loss,
    compute_target,
)


# ---------------------------------------------------------------------------
# Fill simulator
# ---------------------------------------------------------------------------

def test_fill_buy_at_ask_or_above() -> None:
    result = simulate_fill("BUY", Decimal("98"), Decimal("102"), 1, 50)
    assert result.fill_price >= Decimal("102")


def test_fill_sell_at_bid_or_below() -> None:
    result = simulate_fill("SELL", Decimal("98"), Decimal("102"), 1, 50)
    assert result.fill_price <= Decimal("98")


def test_fill_sell_price_floored_at_tick() -> None:
    # Very wide spread: bid=0, ask=1 — sell fill should not go negative
    result = simulate_fill("SELL", Decimal("0"), Decimal("1"), 1, 50)
    assert result.fill_price >= Decimal("0.05")


def test_fill_buy_net_cost_positive() -> None:
    result = simulate_fill("BUY", Decimal("98"), Decimal("102"), 1, 50)
    assert result.net_cost > 0


def test_fill_sell_net_cost_negative() -> None:
    result = simulate_fill("SELL", Decimal("98"), Decimal("102"), 1, 50)
    assert result.net_cost < 0


def test_fill_stt_zero_on_buy() -> None:
    result = simulate_fill("BUY", Decimal("98"), Decimal("102"), 1, 50)
    assert result.stt == Decimal("0")


def test_fill_stt_nonzero_on_sell() -> None:
    result = simulate_fill("SELL", Decimal("98"), Decimal("102"), 1, 50)
    assert result.stt > 0


def test_fill_etc_nonzero_both_sides() -> None:
    buy = simulate_fill("BUY", Decimal("98"), Decimal("102"), 1, 50)
    sell = simulate_fill("SELL", Decimal("98"), Decimal("102"), 1, 50)
    assert buy.etc > 0
    assert sell.etc > 0


def test_fill_quantity_contracts() -> None:
    result = simulate_fill("BUY", Decimal("98"), Decimal("102"), 3, 50)
    assert result.quantity_contracts == 150
    assert result.quantity_lots == 3


def test_total_net_cost_debit_spread() -> None:
    buy = simulate_fill("BUY", Decimal("98"), Decimal("102"), 1, 50)
    sell = simulate_fill("SELL", Decimal("80"), Decimal("84"), 1, 50)
    net = total_net_cost([buy, sell])
    # Net debit: buy is more expensive than what we receive from sell
    assert net > 0


def test_total_net_cost_single_buy() -> None:
    buy = simulate_fill("BUY", Decimal("100"), Decimal("105"), 1, 100)
    assert total_net_cost([buy]) == buy.net_cost


# ---------------------------------------------------------------------------
# Position sizer
# ---------------------------------------------------------------------------

def test_compute_lots_basic() -> None:
    lots = compute_lots(
        portfolio_capital=Decimal("1000000"),  # ₹10 lakh
        max_risk_per_lot=Decimal("1000"),
        lot_size=50,
        atm_premium=Decimal("100"),
    )
    assert lots >= 1


def test_compute_lots_zero_capital() -> None:
    lots = compute_lots(
        portfolio_capital=Decimal("0"),
        max_risk_per_lot=Decimal("1000"),
        lot_size=50,
        atm_premium=Decimal("100"),
    )
    assert lots == 0


def test_compute_lots_zero_risk() -> None:
    lots = compute_lots(
        portfolio_capital=Decimal("1000000"),
        max_risk_per_lot=Decimal("0"),
        lot_size=50,
        atm_premium=Decimal("100"),
    )
    assert lots == 0


def test_compute_lots_high_vix_halves() -> None:
    neutral_lots = compute_lots(
        Decimal("1000000"), Decimal("500"), 50, Decimal("50"),
        vix_regime="neutral",
    )
    high_lots = compute_lots(
        Decimal("1000000"), Decimal("500"), 50, Decimal("50"),
        vix_regime="high",
    )
    # High VIX should give fewer or equal lots
    assert high_lots <= neutral_lots


def test_compute_lots_capped_by_max_position() -> None:
    # With very small risk budget but large capital, the cap should apply
    lots = compute_lots(
        portfolio_capital=Decimal("1000000"),
        max_risk_per_lot=Decimal("1"),          # tiny risk → huge raw_lots
        lot_size=50,
        atm_premium=Decimal("1000"),            # expensive → cap kicks in
        max_position_pct=0.05,
    )
    # max_position = 1000000 * 0.05 = 50000; premium_per_lot = 1000*50=50000 → 1 lot
    assert lots == 1


def test_compute_stop_loss_half_premium() -> None:
    stop = compute_stop_loss(Decimal("100"), "long_call", hard_stop_pct=0.50)
    assert stop == Decimal("50")


def test_compute_stop_loss_floor() -> None:
    stop = compute_stop_loss(Decimal("0.01"), "long_call", hard_stop_pct=0.99)
    assert stop >= Decimal("0.05")


def test_compute_target_low_iv() -> None:
    target = compute_target(Decimal("100"), "long_call", "low", target_multiplier=2.0)
    assert target == Decimal("200")


def test_compute_target_high_iv_capped() -> None:
    target = compute_target(Decimal("100"), "long_call", "high", target_multiplier=3.0)
    assert target <= Decimal("150")  # capped at 1.5x in high IV
