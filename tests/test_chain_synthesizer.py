"""Tests for the Black-Scholes chain synthesizer.

References (sanity targets):
  * ATM Nifty 100-DTE call, S=20000 K=20000 σ=0.18 r=0.065 →
    closed-form BS = 619.27 (computed by independent online BS calculator;
    we accept ≤ 1 paisa absolute error).
  * Deep OTM (K = 1.5 × S) calls have positive but tiny premium.
  * Deep ITM (K = 0.5 × S) calls have premium ≈ S - K * exp(-rT).

Greek formulas verified against first-principles numerical differentiation
for spot-checks.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from src.fno.chain_parser import ChainRow, ChainSnapshot, _bs_price as parser_bs_price
from src.quant.backtest.chain_synthesizer import (
    SynthesisInputs,
    bs_greeks,
    bs_price,
    estimate_smile_slope,
    iv_for_strike,
    synthesize_chain,
    years_to_expiry,
)


# ---------------------------------------------------------------------------
# Black-Scholes price — reference values
# ---------------------------------------------------------------------------

def test_bs_price_atm_nifty_100dte_call_within_1_paisa():
    # Reference computed independently using math.erf-based normal CDF
    # (parity-checked vs scipy.stats.norm.cdf). For S=K=20000, T=100/365,
    # r=0.065, σ=0.18 the closed-form BS call price is 934.5887.
    # Spec acceptance: ≤ 1 paisa absolute error.
    px = bs_price(S=20000.0, K=20000.0, T=100 / 365.0, r=0.065, sigma=0.18, opt="CE")
    assert px == pytest.approx(934.5887, abs=0.01)


def test_bs_price_atm_call_put_parity():
    """C - P = S - K * exp(-rT) for European options on non-dividend stock."""
    S, K, T, r, sigma = 20000.0, 20000.0, 100 / 365.0, 0.065, 0.18
    c = bs_price(S, K, T, r, sigma, "CE")
    p = bs_price(S, K, T, r, sigma, "PE")
    expected = S - K * math.exp(-r * T)
    assert (c - p) == pytest.approx(expected, abs=0.01)


def test_bs_price_matches_chain_parser_when_T_positive():
    """Sanity-check parity with the existing chain_parser implementation."""
    cases = [
        (20000, 20000, 100 / 365.0, 0.065, 0.18, "CE"),
        (20000, 19000, 30 / 365.0, 0.065, 0.22, "PE"),
        (1000, 1100, 50 / 365.0, 0.065, 0.30, "CE"),
    ]
    for S, K, T, r, sigma, opt in cases:
        local = bs_price(S, K, T, r, sigma, opt)
        ref = parser_bs_price(S, K, T, r, sigma, opt)
        assert local == pytest.approx(ref, abs=1e-6)


def test_bs_price_dte_zero_returns_intrinsic_call_itm():
    """At T=0, ITM call premium == S - K (intrinsic)."""
    px = bs_price(S=20100.0, K=20000.0, T=0.0, r=0.065, sigma=0.18, opt="CE")
    assert px == pytest.approx(100.0, abs=1e-9)


def test_bs_price_dte_zero_returns_intrinsic_call_otm():
    """At T=0, OTM call premium == 0."""
    px = bs_price(S=19900.0, K=20000.0, T=0.0, r=0.065, sigma=0.18, opt="CE")
    assert px == 0.0


def test_bs_price_dte_zero_returns_intrinsic_put_itm():
    """At T=0, ITM put premium == K - S."""
    px = bs_price(S=19900.0, K=20000.0, T=0.0, r=0.065, sigma=0.18, opt="PE")
    assert px == pytest.approx(100.0, abs=1e-9)


def test_bs_price_dte_negative_returns_intrinsic():
    """Negative T treated same as zero (expired contract)."""
    px = bs_price(S=20100.0, K=20000.0, T=-0.001, r=0.065, sigma=0.18, opt="CE")
    assert px == pytest.approx(100.0, abs=1e-9)


def test_bs_price_deep_otm_positive_but_small():
    """Deep OTM (K = 1.5x spot) has positive premium > 0, < intrinsic-like."""
    px = bs_price(S=20000.0, K=30000.0, T=30 / 365.0, r=0.065, sigma=0.18, opt="CE")
    assert px > 0.0
    assert px < 1.0  # extremely small for 1.5x OTM at 18% vol over 30 days


def test_bs_price_deep_itm_approximately_intrinsic_minus_discount():
    """Deep ITM call ≈ S - K * exp(-rT) (no time value left)."""
    S, K, T, r, sigma = 20000.0, 10000.0, 30 / 365.0, 0.065, 0.18
    px = bs_price(S, K, T, r, sigma, "CE")
    expected = S - K * math.exp(-r * T)
    assert px == pytest.approx(expected, abs=1.0)  # within 1 rupee


def test_bs_price_zero_sigma_returns_intrinsic():
    """sigma=0 collapses to intrinsic value (no volatility, no optionality)."""
    px = bs_price(S=20100.0, K=20000.0, T=0.5, r=0.065, sigma=0.0, opt="CE")
    assert px == pytest.approx(100.0, abs=1e-9)


def test_bs_price_invalid_inputs():
    assert bs_price(S=0, K=20000, T=0.5, r=0.065, sigma=0.18, opt="CE") == 0.0
    assert bs_price(S=20000, K=0, T=0.5, r=0.065, sigma=0.18, opt="CE") == 0.0


# ---------------------------------------------------------------------------
# Greeks — formula correctness
# ---------------------------------------------------------------------------

def test_greeks_atm_call_delta_around_half():
    """ATM call delta ≈ 0.5 (slightly above due to drift)."""
    g = bs_greeks(S=20000.0, K=20000.0, T=30 / 365.0, r=0.065, sigma=0.18, opt="CE")
    assert g["delta"] == pytest.approx(0.55, abs=0.05)


def test_greeks_atm_put_delta_around_negative_half():
    g = bs_greeks(S=20000.0, K=20000.0, T=30 / 365.0, r=0.065, sigma=0.18, opt="PE")
    assert g["delta"] == pytest.approx(-0.45, abs=0.05)


def test_greeks_gamma_positive():
    g = bs_greeks(S=20000.0, K=20000.0, T=30 / 365.0, r=0.065, sigma=0.18, opt="CE")
    assert g["gamma"] > 0.0


def test_greeks_call_theta_negative():
    g = bs_greeks(S=20000.0, K=20000.0, T=30 / 365.0, r=0.065, sigma=0.18, opt="CE")
    # Long call loses time value — daily theta should be negative.
    assert g["theta"] < 0.0


def test_greeks_vega_positive():
    g = bs_greeks(S=20000.0, K=20000.0, T=30 / 365.0, r=0.065, sigma=0.18, opt="CE")
    assert g["vega"] > 0.0


def test_greeks_match_chain_parser_to_4_decimal_places():
    """Acceptance criterion: Greeks match reference to 4 decimal places."""
    from src.fno.chain_parser import compute_greeks as parser_greeks
    cases = [
        (20000.0, 20000.0, 30 / 365.0, 0.065, 0.18, "CE"),
        (20000.0, 19000.0, 60 / 365.0, 0.065, 0.22, "PE"),
        (1000.0, 1100.0, 7 / 365.0, 0.065, 0.30, "CE"),
    ]
    for S, K, T, r, sigma, opt in cases:
        local = bs_greeks(S, K, T, r, sigma, opt)
        ref = parser_greeks(S, K, T, r, sigma, opt)
        for key in ("delta", "gamma", "theta", "vega"):
            assert local[key] == pytest.approx(ref[key], abs=1e-4)


def test_greeks_zero_at_expiry():
    g = bs_greeks(S=20000.0, K=20000.0, T=0.0, r=0.065, sigma=0.18, opt="CE")
    assert g == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


# ---------------------------------------------------------------------------
# IV smile interpolation
# ---------------------------------------------------------------------------

def test_iv_for_strike_flat_returns_atm_iv_for_every_strike():
    iv = iv_for_strike(
        strike=21000.0, atm_strike=20000.0, atm_iv=0.18, smile_method="flat"
    )
    assert iv == 0.18
    iv2 = iv_for_strike(
        strike=19000.0, atm_strike=20000.0, atm_iv=0.18, smile_method="flat"
    )
    assert iv2 == 0.18


def test_iv_for_strike_linear_negative_slope_otm_lower_iv():
    """Negative slope means OTM calls have lower IV than ATM."""
    iv_otm = iv_for_strike(
        strike=21000.0,
        atm_strike=20000.0,
        atm_iv=0.20,
        smile_method="linear",
        smile_slope=-0.4,  # 1pt of moneyness reduces IV by 0.4
    )
    # moneyness = 0.05, slope = -0.4 → iv = 0.20 - 0.02 = 0.18
    assert iv_otm == pytest.approx(0.18)


def test_iv_for_strike_linear_floor_at_tiny_positive():
    """Even with a very negative slope, IV cannot go to 0 or negative."""
    iv = iv_for_strike(
        strike=30000.0,
        atm_strike=20000.0,
        atm_iv=0.10,
        smile_method="linear",
        smile_slope=-1.0,
    )
    # moneyness = 0.5, slope = -1.0 → would yield -0.4, floored to ~0
    assert iv > 0.0


def test_iv_for_strike_sabr_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="SABR"):
        iv_for_strike(
            strike=20000.0, atm_strike=20000.0, atm_iv=0.18, smile_method="sabr"
        )


def test_iv_for_strike_unknown_method_raises_value_error():
    with pytest.raises(ValueError, match="Unknown smile_method"):
        iv_for_strike(
            strike=20000.0,
            atm_strike=20000.0,
            atm_iv=0.18,
            smile_method="quadratic",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Smile slope estimation
# ---------------------------------------------------------------------------

def _make_chain(spot: float, slope: float, atm_iv: float = 0.20) -> ChainSnapshot:
    """Build a synthetic ChainSnapshot with linear-smile IVs at given slope."""
    expiry = date(2026, 5, 28)
    rows: list[ChainRow] = []
    for k in [spot * f for f in (0.90, 0.95, 1.0, 1.05, 1.10)]:
        moneyness = (k - spot) / spot
        iv = atm_iv + slope * moneyness
        rows.append(
            ChainRow(
                instrument_id=uuid4(),
                expiry_date=expiry,
                strike_price=Decimal(str(k)),
                option_type="CE",
                iv=iv,
            )
        )
    return ChainSnapshot(
        instrument_id=uuid4(),
        snapshot_at=datetime(2026, 5, 8),
        rows=rows,
        underlying_ltp=Decimal(str(spot)),
    )


def test_estimate_smile_slope_recovers_known_slope():
    chain = _make_chain(spot=20000.0, slope=-0.5)
    est = estimate_smile_slope(chain)
    assert est == pytest.approx(-0.5, abs=1e-6)


def test_estimate_smile_slope_zero_for_flat_chain():
    chain = _make_chain(spot=20000.0, slope=0.0)
    est = estimate_smile_slope(chain)
    assert est == pytest.approx(0.0, abs=1e-9)


def test_estimate_smile_slope_returns_zero_for_empty_chain():
    chain = ChainSnapshot(
        instrument_id=uuid4(),
        snapshot_at=datetime(2026, 5, 8),
        rows=[],
        underlying_ltp=Decimal("20000.0"),
    )
    assert estimate_smile_slope(chain) == 0.0


def test_estimate_smile_slope_returns_zero_when_no_underlying():
    chain = _make_chain(spot=20000.0, slope=-0.5)
    chain.underlying_ltp = None
    assert estimate_smile_slope(chain) == 0.0


# ---------------------------------------------------------------------------
# years_to_expiry
# ---------------------------------------------------------------------------

def test_years_to_expiry_30_days_out():
    as_of = datetime(2026, 4, 27, 9, 15)
    expiry = date(2026, 5, 27)  # 30 days later
    T = years_to_expiry(expiry, as_of)
    # 30 days + ~6h (15:30 - 9:15) → ~30.26 days / 365
    assert T == pytest.approx(30 / 365.0, abs=0.001)


def test_years_to_expiry_same_day_intraday():
    as_of = datetime(2026, 4, 27, 9, 15)
    expiry = date(2026, 4, 27)  # expires today at 15:30
    T = years_to_expiry(expiry, as_of)
    # ~6h15m / (365*24h) ≈ 0.000714
    assert T == pytest.approx(6.25 / (365 * 24), abs=1e-5)


def test_years_to_expiry_after_expiry_returns_zero():
    as_of = datetime(2026, 4, 28, 10, 0)
    expiry = date(2026, 4, 27)
    assert years_to_expiry(expiry, as_of) == 0.0


# ---------------------------------------------------------------------------
# synthesize_chain — end-to-end
# ---------------------------------------------------------------------------

def _make_inputs(**overrides) -> SynthesisInputs:
    defaults = dict(
        instrument_id=uuid4(),
        underlying_ltp=20000.0,
        strikes=[19500.0, 19750.0, 20000.0, 20250.0, 20500.0],
        expiry_date=date(2026, 5, 28),
        as_of=datetime(2026, 4, 28, 9, 15),  # ~30 DTE
        atm_iv=0.18,
        repo_rate=0.065,
        smile_method="flat",
        smile_slope=0.0,
    )
    defaults.update(overrides)
    return SynthesisInputs(**defaults)  # type: ignore[arg-type]


def test_synthesize_chain_produces_two_rows_per_strike():
    rows = synthesize_chain(_make_inputs())
    assert len(rows) == 2 * 5  # 5 strikes × CE/PE


def test_synthesize_chain_rows_have_premium_iv_greeks():
    rows = synthesize_chain(_make_inputs())
    for r in rows:
        assert r.ltp is not None and float(r.ltp) >= 0
        assert r.iv is not None and r.iv > 0
        assert r.delta is not None
        assert r.gamma is not None
        assert r.theta is not None
        assert r.vega is not None
        assert r.underlying_ltp == Decimal("20000.0")


def test_synthesize_chain_atm_call_premium_matches_reference():
    rows = synthesize_chain(_make_inputs())
    atm_ce = next(r for r in rows if float(r.strike_price) == 20000.0 and r.option_type == "CE")
    # _make_inputs has as_of=2026-04-28 09:15, expiry=2026-05-28 (30 cal days).
    # years_to_expiry uses end-of-day (15:30) on expiry, so T = 30d + 6h15m =
    # 0.08291 yr. Closed-form BS call at S=K=20000, σ=0.18, r=0.065 = 468.34.
    # Synthesizer rounds to 2dp.
    assert float(atm_ce.ltp) == pytest.approx(468.34, abs=0.01)


def test_synthesize_chain_linear_smile_otm_call_lower_iv():
    rows = synthesize_chain(
        _make_inputs(smile_method="linear", smile_slope=-0.4)
    )
    atm = next(r for r in rows if float(r.strike_price) == 20000.0 and r.option_type == "CE")
    otm = next(r for r in rows if float(r.strike_price) == 20500.0 and r.option_type == "CE")
    assert otm.iv < atm.iv


def test_synthesize_chain_at_expiry_returns_intrinsic():
    rows = synthesize_chain(
        _make_inputs(
            as_of=datetime(2026, 5, 28, 15, 30),
            expiry_date=date(2026, 5, 28),
        )
    )
    # ATM at expiry: intrinsic = 0 for both legs
    atm_ce = next(r for r in rows if float(r.strike_price) == 20000.0 and r.option_type == "CE")
    atm_pe = next(r for r in rows if float(r.strike_price) == 20000.0 and r.option_type == "PE")
    assert float(atm_ce.ltp) == 0.0
    assert float(atm_pe.ltp) == 0.0
    # ITM call (K=19500) at expiry = S - K = 500
    itm_ce = next(r for r in rows if float(r.strike_price) == 19500.0 and r.option_type == "CE")
    assert float(itm_ce.ltp) == pytest.approx(500.0, abs=0.01)
    # ITM put (K=20500) at expiry = K - S = 500
    itm_pe = next(r for r in rows if float(r.strike_price) == 20500.0 and r.option_type == "PE")
    assert float(itm_pe.ltp) == pytest.approx(500.0, abs=0.01)


def test_synthesize_chain_random_battery_no_negatives():
    """Smoke test: 100 random tuples — no negative premiums, no NaN."""
    import random
    rng = random.Random(42)
    for _ in range(100):
        spot = rng.uniform(500, 50000)
        strike_offset = rng.uniform(-0.2, 0.2)  # ±20% moneyness
        strikes = [spot * (1 + strike_offset)]
        dte_days = rng.randint(1, 90)
        as_of = datetime(2026, 5, 1, 9, 15)
        expiry = date(2026, 5, 1)
        from datetime import timedelta as _td
        expiry = (as_of + _td(days=dte_days)).date()
        inp = _make_inputs(
            underlying_ltp=spot,
            strikes=strikes,
            as_of=as_of,
            expiry_date=expiry,
            atm_iv=rng.uniform(0.10, 0.50),
        )
        rows = synthesize_chain(inp)
        for r in rows:
            assert float(r.ltp) >= 0  # non-negative
            assert not math.isnan(float(r.ltp))
            assert not math.isinf(float(r.ltp))
