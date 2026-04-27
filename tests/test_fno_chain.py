"""Tests for chain parser — IV/Greeks computation and chain analytics."""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from src.fno.chain_parser import (
    ChainRow,
    ChainSnapshot,
    classify_oi_buildup,
    compute_greeks,
    compute_iv,
    compute_max_pain,
    compute_pcr,
    enrich_chain_row,
    identify_oi_walls,
)


def _row(strike: float, opt: str, ltp: float | None = None, oi: int = 0,
         bid: float | None = None, ask: float | None = None) -> ChainRow:
    return ChainRow(
        instrument_id=None,
        expiry_date=date(2026, 4, 28),
        strike_price=Decimal(str(strike)),
        option_type=opt,
        ltp=Decimal(str(ltp)) if ltp else None,
        bid_price=Decimal(str(bid)) if bid else None,
        ask_price=Decimal(str(ask)) if ask else None,
        oi=oi,
        underlying_ltp=Decimal("1000"),
    )


# -------------------------------------------------------------------
# Black-Scholes reference (NIFTY ATM call: S=K=1000, T=0.1yr, r=6.5%, σ=20%)
# -------------------------------------------------------------------
S, K, T, r, sigma = 1000.0, 1000.0, 0.1, 0.065, 0.20


def test_compute_greeks_atm_call_delta_near_half() -> None:
    g = compute_greeks(S, K, T, r, sigma, "CE")
    assert 0.45 < g["delta"] < 0.60


def test_compute_greeks_atm_put_delta_near_minus_half() -> None:
    g = compute_greeks(S, K, T, r, sigma, "PE")
    assert -0.60 < g["delta"] < -0.40


def test_compute_greeks_call_put_parity() -> None:
    g_ce = compute_greeks(S, K, T, r, sigma, "CE")
    g_pe = compute_greeks(S, K, T, r, sigma, "PE")
    # Delta CE - Delta PE ≈ 1 (put-call parity)
    assert abs(g_ce["delta"] - g_pe["delta"] - 1.0) < 0.01


def test_compute_iv_round_trip() -> None:
    from src.fno.chain_parser import _bs_price
    market_price = _bs_price(S, K, T, r, sigma, "CE")
    iv = compute_iv(market_price, S, K, T, r, "CE")
    assert iv is not None
    assert abs(iv - sigma) < 0.001, f"IV={iv} expected ~{sigma}"


def test_compute_iv_returns_none_for_zero_T() -> None:
    assert compute_iv(25.0, 1000, 1000, 0.0, 0.065, "CE") is None


def test_enrich_chain_row_fills_greeks() -> None:
    row = _row(1000, "CE", bid=24.0, ask=26.0)
    T = 0.1
    enriched = enrich_chain_row(row, T)
    assert enriched.iv is not None
    assert enriched.delta is not None
    assert 0.0 < enriched.iv < 5.0


def test_enrich_chain_row_skips_zero_T() -> None:
    row = _row(1000, "CE", bid=24.0, ask=26.0)
    enriched = enrich_chain_row(row, T=0.0)
    assert enriched.iv is None


# -------------------------------------------------------------------
# Chain analytics
# -------------------------------------------------------------------

def _make_snapshot() -> ChainSnapshot:
    snap = ChainSnapshot(instrument_id=None, snapshot_at=None)
    snap.underlying_ltp = Decimal("1000")
    snap.rows = [
        _row(900, "CE", oi=10000),
        _row(950, "CE", oi=20000),
        _row(1000, "CE", oi=50000),  # ATM
        _row(1050, "CE", oi=15000),
        _row(900, "PE", oi=5000),
        _row(950, "PE", oi=25000),
        _row(1000, "PE", oi=40000),  # ATM
        _row(1050, "PE", oi=8000),
    ]
    return snap


def test_compute_pcr() -> None:
    snap = _make_snapshot()
    pcr = compute_pcr(snap)
    assert pcr is not None
    ce_oi = 10000 + 20000 + 50000 + 15000
    pe_oi = 5000 + 25000 + 40000 + 8000
    assert abs(pcr - pe_oi / ce_oi) < 0.001


def test_compute_max_pain() -> None:
    snap = _make_snapshot()
    mp = compute_max_pain(snap)
    assert mp is not None
    assert isinstance(mp, Decimal)


def test_identify_oi_walls() -> None:
    snap = _make_snapshot()
    walls = identify_oi_walls(snap, top_n=2)
    assert len(walls["resistance"]) == 2
    assert len(walls["support"]) == 2
    # Top CE OI is at 1000
    assert Decimal("1000") in walls["resistance"]


def test_classify_oi_buildup() -> None:
    assert classify_oi_buildup(100, 98, 5000, 4000) == "long_buildup"
    assert classify_oi_buildup(98, 100, 5000, 4000) == "short_buildup"
    assert classify_oi_buildup(100, 98, 4000, 5000) == "short_covering"
    assert classify_oi_buildup(98, 100, 4000, 5000) == "long_unwinding"


def test_atm_row_returns_closest_strike() -> None:
    snap = _make_snapshot()
    snap.underlying_ltp = Decimal("975")
    atm = snap.atm_row("CE")
    assert atm is not None
    assert atm.strike_price == Decimal("950")  # closest to 975
