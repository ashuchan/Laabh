"""Options chain parser — computes IV, Greeks, PCR, max-pain from raw chain data.

Greeks are computed using Black-Scholes via the `py_vollib` library when not
provided by the feed.  This module is pure-function: no DB access.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Sequence

from loguru import logger

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ChainRow:
    """Normalised options chain row with optional pre-computed Greeks."""

    instrument_id: object
    expiry_date: date
    strike_price: Decimal
    option_type: str  # "CE" or "PE"

    ltp: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    oi: int | None = None
    oi_change: int | None = None

    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None

    underlying_ltp: Decimal | None = None


@dataclass
class ChainSnapshot:
    """All rows for one underlying at one point in time."""

    instrument_id: object
    snapshot_at: object  # datetime
    rows: list[ChainRow] = field(default_factory=list)
    underlying_ltp: Decimal | None = None

    def ce_rows(self, expiry: date | None = None) -> list[ChainRow]:
        rows = [r for r in self.rows if r.option_type == "CE"]
        if expiry:
            rows = [r for r in rows if r.expiry_date == expiry]
        return rows

    def pe_rows(self, expiry: date | None = None) -> list[ChainRow]:
        rows = [r for r in self.rows if r.option_type == "PE"]
        if expiry:
            rows = [r for r in rows if r.expiry_date == expiry]
        return rows

    def atm_row(self, option_type: str, expiry: date | None = None) -> ChainRow | None:
        """Return the ATM strike row (closest to underlying_ltp)."""
        if self.underlying_ltp is None:
            return None
        relevant = self.ce_rows(expiry) if option_type == "CE" else self.pe_rows(expiry)
        if not relevant:
            return None
        ltp = float(self.underlying_ltp)
        return min(relevant, key=lambda r: abs(float(r.strike_price) - ltp))


# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, opt: str) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def compute_iv(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    opt: str,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> float | None:
    """Compute implied volatility using bisection method."""
    if T <= 0 or market_price <= 0:
        return None
    lo, hi = 1e-6, 10.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        price = _bs_price(S, K, T, r, mid, opt)
        if abs(price - market_price) < tol:
            return mid
        if price < market_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def compute_greeks(
    S: float, K: float, T: float, r: float, sigma: float, opt: str
) -> dict[str, float]:
    """Compute Delta, Gamma, Theta, Vega for a European option."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "CE":
        delta = _norm_cdf(d1)
        theta = (
            -(S * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T))
            - r * K * math.exp(-r * T) * _norm_cdf(d2)
        ) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(S * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T))
            + r * K * math.exp(-r * T) * _norm_cdf(-d2)
        ) / 365.0
    gamma = _norm_pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * _norm_pdf(d1) * math.sqrt(T) / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


# ---------------------------------------------------------------------------
# Chain-level analytics
# ---------------------------------------------------------------------------

def compute_pcr(snapshot: ChainSnapshot, expiry: date | None = None) -> float | None:
    """Put-call ratio by OI."""
    total_pe_oi = sum(
        (r.oi or 0) for r in snapshot.pe_rows(expiry)
    )
    total_ce_oi = sum(
        (r.oi or 0) for r in snapshot.ce_rows(expiry)
    )
    if total_ce_oi == 0:
        return None
    return total_pe_oi / total_ce_oi


def compute_max_pain(snapshot: ChainSnapshot, expiry: date | None = None) -> Decimal | None:
    """Max-pain strike: minimise total option-writer pain at expiry."""
    all_strikes = sorted({r.strike_price for r in snapshot.rows if r.expiry_date == expiry or expiry is None})
    if not all_strikes:
        return None

    ce_rows = {r.strike_price: r for r in snapshot.ce_rows(expiry)}
    pe_rows = {r.strike_price: r for r in snapshot.pe_rows(expiry)}

    min_pain = float("inf")
    max_pain_strike = all_strikes[0]

    for S in all_strikes:
        S_f = float(S)
        pain = 0.0
        for strike, row in ce_rows.items():
            pain += max(0.0, S_f - float(strike)) * (row.oi or 0)
        for strike, row in pe_rows.items():
            pain += max(0.0, float(strike) - S_f) * (row.oi or 0)
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = S

    return max_pain_strike


def identify_oi_walls(
    snapshot: ChainSnapshot, expiry: date | None = None, top_n: int = 3
) -> dict[str, list[Decimal]]:
    """Return top call OI strikes (resistance) and top put OI strikes (support)."""
    ce_by_oi = sorted(
        [(r.strike_price, r.oi or 0) for r in snapshot.ce_rows(expiry)],
        key=lambda x: x[1], reverse=True
    )
    pe_by_oi = sorted(
        [(r.strike_price, r.oi or 0) for r in snapshot.pe_rows(expiry)],
        key=lambda x: x[1], reverse=True
    )
    return {
        "resistance": [s for s, _ in ce_by_oi[:top_n]],
        "support": [s for s, _ in pe_by_oi[:top_n]],
    }


def classify_oi_buildup(
    current_price: float,
    prev_price: float,
    current_oi: int,
    prev_oi: int,
) -> str:
    """Classify OI movement: long_buildup | short_buildup | long_unwinding | short_covering."""
    price_up = current_price > prev_price
    oi_up = current_oi > prev_oi
    if price_up and oi_up:
        return "long_buildup"
    if price_up and not oi_up:
        return "short_covering"
    if not price_up and oi_up:
        return "short_buildup"
    return "long_unwinding"


def enrich_chain_row(
    row: ChainRow,
    T: float,  # time to expiry in years
    r: float = 0.065,  # risk-free rate (RBI repo ~6.5%)
) -> ChainRow:
    """Fill in IV and Greeks if not already populated."""
    S = float(row.underlying_ltp or 0)
    K = float(row.strike_price)
    opt = row.option_type

    if S <= 0 or K <= 0 or T <= 0:
        return row

    # Compute IV from mid-price if not provided
    if row.iv is None:
        mid = None
        if row.bid_price is not None and row.ask_price is not None:
            mid = (float(row.bid_price) + float(row.ask_price)) / 2.0
        elif row.ltp is not None:
            mid = float(row.ltp)
        if mid and mid > 0:
            iv = compute_iv(mid, S, K, T, r, opt)
            row.iv = iv

    # Compute Greeks if IV is now known
    if row.iv and row.delta is None:
        greeks = compute_greeks(S, K, T, r, row.iv, opt)
        row.delta = greeks["delta"]
        row.gamma = greeks["gamma"]
        row.theta = greeks["theta"]
        row.vega = greeks["vega"]

    return row
