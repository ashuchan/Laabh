"""Layer 3 — Position sizer: half-Kelly + exposure caps + cost gate.

Algorithm (per spec §8):
 1. p  = sigmoid(posterior_mean) clamped to [0.05, 0.95]
 2. b  = empirical win/loss ratio (default 1.5 for cold-start)
 3. f_kelly = (p*b - (1-p)) / b
 4. f  = KELLY_FRACTION × f_kelly
 5. f  = clamp(f, 0, MAX_PER_TRADE_PCT)
 6. risk_budget = capital × f
 7. raw_lots = floor(risk_budget / max_loss_per_lot)
 8. cap by total open exposure ≤ MAX_TOTAL_EXPOSURE_PCT
 9. cost gate: if expected_gross < COST_GATE × estimated_costs → 0 lots
10. lock-in reduction if active
"""
from __future__ import annotations

import math
from decimal import Decimal

from src.config import get_settings


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def compute_lots(
    *,
    posterior_mean: float,
    portfolio_capital: Decimal,
    max_loss_per_lot: Decimal,
    estimated_costs: Decimal,
    expected_gross_pnl: Decimal,
    open_exposure: Decimal,
    lockin_active: bool,
    win_loss_ratio: float = 1.5,
    as_of=None,
    dryrun_run_id=None,
) -> int:
    """Return the number of lots to trade (0 means skip).

    Args:
        posterior_mean: The arm's current posterior mean return estimate.
        portfolio_capital: Total portfolio capital (Decimal, INR).
        max_loss_per_lot: Max premium risked per lot (Decimal, INR).
        estimated_costs: Brokerage + STT + slippage per lot (Decimal, INR).
        expected_gross_pnl: Expected gross P&L per lot from signal (Decimal, INR).
        open_exposure: Current total open position premium (Decimal, INR).
        lockin_active: True if the day's lock-in has fired (halves f_max).
        win_loss_ratio: b in Kelly formula. Default 1.5 (cold-start).
        as_of: Ignored; accepted for pipeline convention.
        dryrun_run_id: Ignored; accepted for pipeline convention.
    """
    settings = get_settings()

    # Step 1: probability of win via sigmoid on posterior mean
    p = max(0.05, min(0.95, _sigmoid(posterior_mean * 10)))  # ×10 to move away from 0.5

    # Step 2: win/loss ratio
    b = win_loss_ratio

    # Step 3: Kelly fraction
    f_kelly = (p * b - (1 - p)) / b

    # Step 4: half-Kelly
    f = settings.laabh_quant_kelly_fraction * f_kelly

    # Step 5: clamp to per-trade cap (apply lock-in reduction if active)
    max_pct = settings.laabh_quant_max_per_trade_pct
    if lockin_active:
        max_pct *= settings.laabh_quant_lockin_size_reduction
    f = max(0.0, min(f, max_pct))

    if f <= 0 or portfolio_capital <= 0 or max_loss_per_lot <= 0:
        return 0

    # Step 6: risk budget
    risk_budget = Decimal(str(f)) * portfolio_capital

    # Step 7: raw lots
    raw_lots = int(risk_budget / max_loss_per_lot)
    if raw_lots == 0:
        return 0

    # Step 8: total-exposure cap
    max_exposure = Decimal(str(settings.laabh_quant_max_total_exposure_pct)) * portfolio_capital
    remaining_exposure = max_exposure - open_exposure
    if remaining_exposure <= 0:
        return 0
    if max_loss_per_lot > 0:
        lots_by_exposure = int(remaining_exposure / max_loss_per_lot)
        raw_lots = min(raw_lots, lots_by_exposure)

    if raw_lots == 0:
        return 0

    # Step 9: cost gate — gross P&L must exceed multiple × costs
    cost_gate = Decimal(str(settings.laabh_quant_cost_gate_multiple))
    if expected_gross_pnl < cost_gate * estimated_costs:
        return 0

    return raw_lots
