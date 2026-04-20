"""Tests for the paper trading engine — order execution and brokerage charges."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.trading.engine import TradingEngine, _round


def test_calc_charges_buy():
    engine = TradingEngine()
    qty = 10
    price = Decimal("1000.00")
    brokerage, stt, other = engine._calc_charges("BUY", qty, price)

    # Turnover = 10000; brokerage = min(10000 * 0.0003, 20) = 3
    assert brokerage == pytest.approx(float(_round(Decimal("3") * Decimal("1.18"))), abs=0.01)
    assert float(stt) == 0.0  # No STT on BUY for delivery


def test_calc_charges_sell_delivery():
    engine = TradingEngine()
    qty = 10
    price = Decimal("1000.00")
    brokerage, stt, other = engine._calc_charges("SELL", qty, price)

    turnover = Decimal("10000")
    expected_stt = turnover * Decimal("0.001")
    assert float(stt) == pytest.approx(float(_round(expected_stt)), abs=0.01)


def test_calc_charges_brokerage_cap():
    engine = TradingEngine()
    # Large order where brokerage would exceed ₹20 cap
    qty = 1000
    price = Decimal("10000.00")  # turnover = 1 crore
    brokerage, stt, other = engine._calc_charges("BUY", qty, price)

    # Raw brokerage = 0.03% of 1cr = 3000 > ₹20 → capped at 20 + 18% GST = 23.60
    assert float(brokerage) == pytest.approx(23.60, abs=0.01)


def test_round_decimal():
    assert _round(Decimal("3.145")) == Decimal("3.15")
    assert _round(Decimal("3.144")) == Decimal("3.14")
