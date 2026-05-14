"""Greeks Engine — Black-Scholes Greeks for open F&O positions.

Computes portfolio-level net delta, gamma, theta, and vega by aggregating
option Greeks across all open fno_signals positions. Uses a pure-Python
Black-Scholes implementation (no scipy dependency) based on the math.erfc
approximation for the Gaussian CDF.

Design decisions:
  - Greeks computed at runtime from current chain data (not stored per-leg).
    Storing Greeks at entry would require updating them continuously; runtime
    computation always uses the freshest available IV and spot.

  - Lot size is 1 for all instruments (DB default never populated). Greeks are
    therefore in per-lot terms and cannot be expressed in absolute notional.
    The entry gate uses budget fractions relative to portfolio_value rather
    than absolute Greek units — this makes the check lot-size independent.

  - Warn-only initially: the entry gate logs violations but does NOT reject
    entries. This allows data to accumulate so we can calibrate sensible
    budget thresholds before hardening the gate.

  - Time to expiry: uses trading days (T = dte / 252) for theta consistency.
    A 30-calendar-day option with 22 trading days remaining has T = 22/252.

Integration:
  - entry_engine.propose_entries() calls check_entry_fits() before including
    a proposal. Currently warn-only; set FNO_GREEKS_HARD_GATE=true to block.
  - Called from fno_phase4_manage and fno_phase4_entry scheduler jobs.
  - Logs a portfolio_greeks_log row every invocation for monitoring.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Sequence

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import get_settings
from src.db import session_scope


# ---------------------------------------------------------------------------
# Pure Black-Scholes implementation (no scipy)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the complementary error function (math.erfc).
    Accurate to ~7 significant figures — sufficient for options Greeks.
    """
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(
    S: float,      # spot price
    K: float,      # strike price
    T: float,      # time to expiry in calendar-year fraction (dte_calendar / 365)
    r: float,      # risk-free rate (annualized decimal, e.g. 0.065)
    sigma: float,  # annualized IV (decimal, e.g. 0.25)
    option_type: str,  # 'CE' or 'PE'
) -> tuple[float, float, float, float] | None:
    """Compute (delta, gamma, theta_per_day, vega_per_1pct) or None on invalid inputs.

    Returns:
        delta: sensitivity to ₹1 move in spot
        gamma: change in delta per ₹1 move in spot (same for CE and PE)
        theta: P&L per calendar day from time decay (negative for longs)
        vega:  P&L per 1% (0.01) change in IV

    Sign convention (from BUYER's perspective — sign is flipped for short legs
    in the portfolio aggregation):
        CE delta:  +0 to +1
        PE delta:  -1 to 0
        gamma:     always positive for both CE and PE (long positions)
        theta:     always negative for both CE and PE (long positions decay)
        vega:      always positive (long options gain when IV rises)
    """
    if T <= 1e-6 or sigma <= 1e-6 or S <= 0 or K <= 0:
        return None

    try:
        sqrtT = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
        d2 = d1 - sigma * sqrtT

        gamma = _norm_pdf(d1) / (S * sigma * sqrtT)
        vega = S * _norm_pdf(d1) * sqrtT / 100.0   # per 1% IV change (per 0.01)

        if option_type == "CE":
            delta = _norm_cdf(d1)
            # Theta per calendar day (divide by 365, not 252, for calendar-day theta)
            theta = (
                -S * _norm_pdf(d1) * sigma / (2.0 * sqrtT)
                - r * K * math.exp(-r * T) * _norm_cdf(d2)
            ) / 365.0
        else:  # PE
            delta = _norm_cdf(d1) - 1.0
            theta = (
                -S * _norm_pdf(d1) * sigma / (2.0 * sqrtT)
                + r * K * math.exp(-r * T) * _norm_cdf(-d2)
            ) / 365.0

        return delta, gamma, theta, vega

    except (ValueError, ZeroDivisionError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Portfolio Greeks dataclass
# ---------------------------------------------------------------------------

@dataclass
class PortfolioGreeks:
    open_positions: int = 0
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_theta: float = 0.0
    net_vega: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def delta_budget_used(self, budget: float) -> float:
        return abs(self.net_delta) / budget if budget > 0 else 0.0

    def vega_budget_used(self, budget: float) -> float:
        return abs(self.net_vega) / budget if budget > 0 else 0.0

    def summary(self) -> str:
        return (
            f"positions={self.open_positions} "
            f"net_delta={self.net_delta:+.3f} "
            f"net_gamma={self.net_gamma:+.6f} "
            f"net_theta={self.net_theta:+.2f}/day "
            f"net_vega={self.net_vega:+.2f}/1pct"
        )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_open_positions(session) -> list[dict]:
    """Fetch all open fno_signals with their leg definitions."""
    rows = (await session.execute(text("""
        SELECT s.id, s.underlying_id, s.expiry_date, s.legs,
               s.strategy_type, i.symbol
        FROM fno_signals s
        JOIN instruments i ON i.id = s.underlying_id
        WHERE s.status IN ('live', 'active', 'open', 'proposed')
          AND s.dryrun_run_id IS NULL
          AND s.closed_at IS NULL
    """))).fetchall()
    return [dict(r._mapping) for r in rows]


def _normalize_iv(raw: float | None) -> float | None:
    """Normalize chain IV to annualized decimal (0.25 = 25% vol).

    Canonical version — identical threshold as vrp_engine._to_decimal_iv.
    Do NOT duplicate this logic. Change both or extract to a shared util.
    Any Indian-listed equity IV > 300% annual (decimal 3.0) is impossible,
    so values > 3.0 are treated as percentage-point form and divided by 100.
    """
    if raw is None or raw <= 0.0:
        return None
    return raw / 100.0 if raw > 3.0 else raw


async def _get_iv_for_leg(session, instrument_id: str, expiry: date, strike: float, option_type: str) -> float | None:
    """Fetch the latest IV for a specific leg from options_chain."""
    from src.models.fno_chain import OptionsChain
    row = (await session.execute(
        select(OptionsChain.iv, OptionsChain.underlying_ltp)
        .where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.expiry_date == expiry,
            OptionsChain.strike_price == strike,
            OptionsChain.option_type == option_type,
            OptionsChain.iv.isnot(None),
        )
        .order_by(OptionsChain.snapshot_at.desc())
        .limit(1)
    )).first()
    if row is None or row.iv is None:
        return None
    return _normalize_iv(float(row.iv))


async def _get_spot_for_instrument(session, instrument_id: str) -> float | None:
    """Get the latest underlying LTP from options_chain."""
    from src.models.fno_chain import OptionsChain
    row = (await session.execute(
        select(OptionsChain.underlying_ltp)
        .where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.underlying_ltp > 0,
        )
        .order_by(OptionsChain.snapshot_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    return float(row) if row else None


async def _log_greeks(session, portfolio_id, pg: PortfolioGreeks, cfg) -> None:
    await session.execute(text("""
        INSERT INTO portfolio_greeks_log
            (portfolio_id, open_positions, net_delta, net_gamma, net_theta, net_vega,
             delta_budget_used, vega_budget_used, budget_warnings)
        VALUES
            (:pid, :n, :delta, :gamma, :theta, :vega, :du, :vu, :warns::jsonb)
    """), {
        "pid": portfolio_id,
        "n": pg.open_positions,
        "delta": pg.net_delta,
        "gamma": pg.net_gamma,
        "theta": pg.net_theta,
        "vega": pg.net_vega,
        "du": pg.delta_budget_used(cfg.fno_greeks_delta_budget),
        "vu": pg.vega_budget_used(cfg.fno_greeks_vega_budget),
        "warns": json.dumps(pg.warnings),
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def compute_portfolio_greeks() -> PortfolioGreeks:
    """Aggregate BS Greeks across all open fno_signals positions.

    Returns PortfolioGreeks with net exposures. Positions with missing
    chain data (IV or spot unavailable) are skipped with a warning.
    """
    cfg = get_settings()
    rfr = cfg.fno_risk_free_rate_pct / 100.0   # e.g. 6.5% → 0.065
    today = date.today()
    pg = PortfolioGreeks()

    async with session_scope() as session:
        positions = await _get_open_positions(session)

    if not positions:
        return pg

    for pos in positions:
        inst_id = str(pos["underlying_id"])
        expiry: date = pos["expiry_date"]
        legs_raw = pos["legs"]
        if isinstance(legs_raw, str):
            try:
                legs_raw = json.loads(legs_raw)
            except Exception:
                continue

        dte = (expiry - today).days
        if dte <= 0:
            continue  # expired — skip
        # T in calendar-year fraction (matches the /365 divisor in bs_greeks theta).
        # Using calendar days (not trading days) is standard for options time-value
        # and is consistent with the theta formula dividing by 365.
        T = dte / 365.0

        async with session_scope() as session:
            spot = await _get_spot_for_instrument(session, inst_id)

        if spot is None:
            pg.warnings.append(f"{pos['symbol']}: no spot data")
            continue

        for leg in legs_raw:
            try:
                strike = float(leg["strike"])
                opt_type = leg["option_type"]   # 'CE' or 'PE'
                action = leg.get("action", "BUY")   # 'BUY' or 'SELL'
                qty = int(leg.get("quantity", 1))
                sign = 1 if action == "BUY" else -1

                async with session_scope() as session:
                    iv = await _get_iv_for_leg(session, inst_id, expiry, strike, opt_type)

                if iv is None:
                    pg.warnings.append(f"{pos['symbol']} {opt_type} {strike:.0f}: no IV")
                    continue

                greeks = bs_greeks(spot, strike, T, rfr, iv, opt_type)
                if greeks is None:
                    continue

                delta, gamma, theta, vega = greeks
                pg.net_delta += sign * qty * delta
                pg.net_gamma += sign * qty * gamma
                pg.net_theta += sign * qty * theta
                pg.net_vega += sign * qty * vega

            except (KeyError, ValueError, TypeError) as exc:
                pg.warnings.append(f"{pos['symbol']} leg parse error: {exc}")

        pg.open_positions += 1

    return pg


async def check_entry_fits(
    proposal_greeks: tuple[float, float, float, float],
    current_portfolio: PortfolioGreeks | None = None,
) -> tuple[bool, list[str]]:
    """Check whether a new position's Greeks keep the portfolio within budgets.

    Returns (fits: bool, reasons: list[str]).
    When FNO_GREEKS_HARD_GATE=false (default), always returns True but still
    logs violations — this allows data accumulation before hardening.

    proposal_greeks: (delta, gamma, theta, vega) for the proposed new position.
    """
    cfg = get_settings()
    hard_gate = cfg.fno_greeks_hard_gate

    if current_portfolio is None:
        current_portfolio = await compute_portfolio_greeks()

    p_delta, p_gamma, p_theta, p_vega = proposal_greeks
    new_delta = current_portfolio.net_delta + p_delta
    new_vega = current_portfolio.net_vega + p_vega
    new_theta = current_portfolio.net_theta + p_theta

    violations: list[str] = []

    if abs(new_delta) > cfg.fno_greeks_delta_budget:
        violations.append(
            f"delta budget breach: |{new_delta:.3f}| > {cfg.fno_greeks_delta_budget} "
            f"(adding delta={p_delta:+.3f})"
        )

    if new_vega < -cfg.fno_greeks_vega_budget:
        violations.append(
            f"vega budget breach: {new_vega:.2f} < -{cfg.fno_greeks_vega_budget} "
            f"(adding vega={p_vega:+.2f})"
        )

    if new_theta < -cfg.fno_greeks_min_theta:
        violations.append(
            f"theta floor breach: {new_theta:.2f} < -{cfg.fno_greeks_min_theta}/day "
            f"(adding theta={p_theta:+.2f})"
        )

    if violations:
        for v in violations:
            logger.warning(f"greeks_engine: {'GATE VIOLATION' if hard_gate else 'WARN'} {v}")

    fits = len(violations) == 0 or not hard_gate
    return fits, violations


async def snapshot_portfolio_greeks(portfolio_id=None) -> PortfolioGreeks:
    """Compute and log portfolio Greeks. Called from scheduler every 5 min."""
    cfg = get_settings()
    pg = await compute_portfolio_greeks()

    if pg.warnings:
        logger.debug(f"greeks_engine: {len(pg.warnings)} positions skipped: {pg.warnings[:3]}")

    if pg.open_positions > 0:
        logger.info(f"greeks_engine: {pg.summary()}")

    try:
        async with session_scope() as session:
            await _log_greeks(session, portfolio_id, pg, cfg)
    except Exception as exc:
        logger.debug(f"greeks_engine: log failed: {exc!r}")

    return pg
