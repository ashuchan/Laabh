"""Synthesize intraday option-chain rows from underlying LTP + daily IV.

Used by ``BacktestFeatureStore`` (Task 8) to fabricate Tier 3 option premiums
for any (virtual_time, underlying, strike, expiry) tuple. Real intraday IV
moves are not captured — within a backtest day we hold IV constant at the
morning's ``atm_iv`` and apply a configurable smile.

Smile methods:
  * ``flat``   — every strike uses ``atm_iv`` (cheap and crude).
  * ``linear`` — ``iv = atm_iv + slope * (K - K_atm) / K_atm``, where
                 ``slope`` is derived from the prior day's chain by the
                 caller and passed in. Captures most of the typical equity
                 vol skew with one parameter.
  * ``sabr``   — explicit ``NotImplementedError``; deferred to v2 per spec.

Decision Note (math implementation):
  * The Black-Scholes math here is *intentionally local* rather than reused
    from ``src/fno/chain_parser.py`` because that module's ``_bs_price``
    short-circuits to 0 for any ``T <= 0``. The synthesizer needs to return
    *intrinsic value* at expiry (T=0), so we implement BS inline with an
    explicit T=0 branch. We test for parity with chain_parser for T>0.
  * Inputs use IST-aware ``as_of`` for DTE; output uses ``Decimal`` for
    monetary fields (matches ``ChainRow`` shape) and ``float`` for IV/Greeks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Literal, Sequence
from uuid import UUID

from src.fno.chain_parser import ChainRow, ChainSnapshot

# Trading days in a year — used to convert calendar DTE to year-fraction.
# We use 365 (calendar days) rather than 252 (trading days) because BS time
# is calendar-time (theta decays on weekends and holidays in pricing terms).
_DAYS_PER_YEAR = 365.0


SmileMethod = Literal["flat", "linear", "sabr"]


# ---------------------------------------------------------------------------
# Local Black-Scholes (handles T=0 intrinsic; chain_parser._bs_price doesn't)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float, opt: str
) -> float:
    """Black-Scholes European option price.

    For ``T <= 0`` returns intrinsic value (this is the key difference from
    ``src.fno.chain_parser._bs_price`` which returns 0 unconditionally for
    expired contracts). For ``sigma <= 0`` or ``S <= 0`` returns intrinsic.
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        if opt == "CE":
            return max(0.0, S - K)
        return max(0.0, K - S)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    if opt == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_greeks(
    S: float, K: float, T: float, r: float, sigma: float, opt: str
) -> dict[str, float]:
    """Delta, Gamma, Theta (per day), Vega (per 1 vol-pt) for a European option.

    Theta returned is the *daily* theta (per-calendar-day). Vega is per
    1 percentage-point of vol (vol unit = 0.01).
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = _norm_pdf(d1)
    if opt == "CE":
        delta = _norm_cdf(d1)
        theta = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_T)
            - r * K * math.exp(-r * T) * _norm_cdf(d2)
        ) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_T)
            + r * K * math.exp(-r * T) * _norm_cdf(-d2)
        ) / 365.0
    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


# ---------------------------------------------------------------------------
# Smile interpolation
# ---------------------------------------------------------------------------

def iv_for_strike(
    *,
    strike: float,
    atm_strike: float,
    atm_iv: float,
    smile_method: SmileMethod,
    smile_slope: float = 0.0,
) -> float:
    """Return the IV to use for ``strike`` under the chosen smile method.

    Args:
        strike: The strike whose IV we need.
        atm_strike: ATM strike (closest strike to spot).
        atm_iv: ATM IV (the morning's reading, held constant intraday).
        smile_method: Which smile to apply.
        smile_slope: Linear-method slope, derived from prior-day chain by the
            caller. Ignored for ``flat``. Required for ``linear``.

    Returns:
        Positive float IV. Floored at 1e-6 to keep BS math finite.
    """
    if smile_method == "flat":
        return max(1e-6, atm_iv)
    if smile_method == "linear":
        if atm_strike <= 0:
            return max(1e-6, atm_iv)
        moneyness = (strike - atm_strike) / atm_strike
        return max(1e-6, atm_iv + smile_slope * moneyness)
    if smile_method == "sabr":
        # Spec §3 explicitly defers SABR to v2. Fail loud rather than
        # silently substituting linear so the config validator surfaces it.
        raise NotImplementedError(
            "SABR smile is deferred to v2 of the backtest harness. "
            "Use smile_method='flat' or 'linear'."
        )
    raise ValueError(f"Unknown smile_method: {smile_method!r}")


def estimate_smile_slope(
    prior_chain: ChainSnapshot,
    *,
    expiry_date: date | None = None,
    option_type: str = "CE",
) -> float:
    """Estimate linear-smile slope from a prior-day ``ChainSnapshot``.

    Uses a least-squares fit ``iv ~ moneyness``. Falls back to 0.0 (flat)
    when the chain has fewer than 2 strikes with usable IV — the caller
    should be ready for that.

    The slope is computed against ``moneyness = (K - K_atm) / K_atm`` so it
    can be applied identically to today's chain regardless of spot drift.
    """
    if prior_chain.underlying_ltp is None:
        return 0.0
    rows: list[ChainRow]
    if option_type == "CE":
        rows = prior_chain.ce_rows(expiry_date)
    else:
        rows = prior_chain.pe_rows(expiry_date)
    spot = float(prior_chain.underlying_ltp)
    if not rows or spot <= 0:
        return 0.0
    # Find ATM strike (closest to spot)
    rows_with_iv = [r for r in rows if r.iv is not None and float(r.strike_price) > 0]
    if len(rows_with_iv) < 2:
        return 0.0
    atm_row = min(rows_with_iv, key=lambda r: abs(float(r.strike_price) - spot))
    atm_strike = float(atm_row.strike_price)
    if atm_strike <= 0:
        return 0.0

    xs: list[float] = []
    ys: list[float] = []
    for r in rows_with_iv:
        K = float(r.strike_price)
        m = (K - atm_strike) / atm_strike
        xs.append(m)
        ys.append(float(r.iv))  # type: ignore[arg-type]
    if len(xs) < 2:
        return 0.0

    # OLS slope of y on x: cov(x, y) / var(x)
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var = sum((x - mean_x) ** 2 for x in xs)
    if var == 0:
        return 0.0
    return cov / var


# ---------------------------------------------------------------------------
# Public API — synthesize a chain at a virtual instant
# ---------------------------------------------------------------------------

@dataclass
class SynthesisInputs:
    """All inputs needed to synthesize an intraday chain for one underlying.

    The caller (BacktestFeatureStore) builds this once per (underlying, day)
    and reuses it across the day's ticks, varying ``underlying_ltp`` and
    ``as_of`` per tick.
    """

    instrument_id: UUID
    underlying_ltp: float
    strikes: Sequence[float]
    expiry_date: date
    as_of: datetime           # virtual instant — used for DTE
    atm_iv: float             # morning's ATM IV (held constant intraday)
    repo_rate: float          # decimal (e.g. 0.065 for 6.5%)
    smile_method: SmileMethod = "linear"
    smile_slope: float = 0.0


def years_to_expiry(expiry_date: date, as_of: datetime) -> float:
    """Calendar-year fraction from ``as_of`` to end-of-day on ``expiry_date``.

    Calendar-day BS convention — theta decays even on weekends/holidays.
    Returns 0.0 if the expiry has already passed.
    """
    # Use end-of-day on the expiry date as the effective expiry instant.
    expiry_dt = datetime.combine(expiry_date, datetime.min.time())
    expiry_dt = expiry_dt.replace(hour=15, minute=30)
    if as_of.tzinfo is not None:
        # Strip tz to compare with naive expiry_dt — both are in IST equivalent.
        as_of_naive = as_of.replace(tzinfo=None)
    else:
        as_of_naive = as_of
    diff_seconds = (expiry_dt - as_of_naive).total_seconds()
    return max(0.0, diff_seconds / (_DAYS_PER_YEAR * 86400.0))


def _atm_strike(strikes: Iterable[float], spot: float) -> float:
    """Return the strike closest to ``spot``."""
    strike_list = list(strikes)
    if not strike_list:
        return spot
    return min(strike_list, key=lambda k: abs(k - spot))


def synthesize_chain(inp: SynthesisInputs) -> list[ChainRow]:
    """Return a list of ``ChainRow`` (CE and PE per strike) for ``inp``.

    Rows are populated with:
      * ``ltp``    = synthesized BS premium
      * ``iv``     = smile-adjusted IV
      * ``delta``, ``gamma``, ``theta``, ``vega``
      * ``underlying_ltp`` from ``inp``

    Bid/ask, OI, and volume are *not* synthesized here — Tier 3 limits per
    spec §2.1. The caller layers a spread model on top if needed.
    """
    T = years_to_expiry(inp.expiry_date, inp.as_of)
    atm_strike = _atm_strike(inp.strikes, inp.underlying_ltp)
    rows: list[ChainRow] = []
    for k in inp.strikes:
        for opt in ("CE", "PE"):
            iv = iv_for_strike(
                strike=k,
                atm_strike=atm_strike,
                atm_iv=inp.atm_iv,
                smile_method=inp.smile_method,
                smile_slope=inp.smile_slope,
            )
            premium = bs_price(inp.underlying_ltp, k, T, inp.repo_rate, iv, opt)
            greeks = bs_greeks(inp.underlying_ltp, k, T, inp.repo_rate, iv, opt)
            rows.append(
                ChainRow(
                    instrument_id=inp.instrument_id,
                    expiry_date=inp.expiry_date,
                    strike_price=Decimal(str(k)),
                    option_type=opt,
                    ltp=Decimal(str(round(premium, 2))),
                    iv=iv,
                    delta=greeks["delta"],
                    gamma=greeks["gamma"],
                    theta=greeks["theta"],
                    vega=greeks["vega"],
                    underlying_ltp=Decimal(str(inp.underlying_ltp)),
                )
            )
    return rows
