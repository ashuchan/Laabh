"""Tests for quant position sizer."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.quant.sizer import compute_lots


def _compute(**overrides):
    defaults = dict(
        posterior_mean=0.02,
        portfolio_capital=Decimal("1000000"),
        max_loss_per_lot=Decimal("5000"),
        estimated_costs=Decimal("200"),
        expected_gross_pnl=Decimal("2000"),
        open_exposure=Decimal("0"),
        lockin_active=False,
        # Explicit sizing params (mirrors config defaults)
        kelly_fraction=0.5,
        max_per_trade_pct=0.03,
        lockin_size_reduction=0.5,
        max_total_exposure_pct=0.30,
        cost_gate_multiple=3.0,
    )
    defaults.update(overrides)
    return compute_lots(**defaults)


def test_positive_lots_on_good_setup():
    lots = _compute()
    assert lots >= 1


def test_cost_gate_blocks_trade():
    # expected_gross = 300 < 3 × 200 = 600 → 0 lots
    lots = _compute(expected_gross_pnl=Decimal("300"))
    assert lots == 0


def test_exposure_cap():
    # open_exposure already at 30% of capital → no room
    lots = _compute(open_exposure=Decimal("300000"))
    assert lots == 0


def test_zero_on_negative_kelly():
    # posterior_mean very negative → f_kelly negative → 0
    lots = _compute(posterior_mean=-2.0)
    assert lots == 0


def test_zero_on_zero_capital():
    lots = _compute(portfolio_capital=Decimal("0"))
    assert lots == 0


def test_lockin_reduces_lots():
    lots_normal = _compute(lockin_active=False)
    lots_lockin = _compute(lockin_active=True)
    assert lots_lockin <= lots_normal


def test_decimal_arithmetic_no_float_error():
    # All Decimal inputs — should not raise
    lots = _compute(
        portfolio_capital=Decimal("500000.50"),
        max_loss_per_lot=Decimal("4999.99"),
        estimated_costs=Decimal("150.00"),
        expected_gross_pnl=Decimal("1500.00"),
    )
    assert isinstance(lots, int)
