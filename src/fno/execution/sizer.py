"""Position sizer — computes how many lots to trade given capital and risk limits.

Sizing algorithm:
  1. risk_budget = portfolio_capital × fno_sizing_risk_per_trade_pct
     (default 1% of capital per trade)
  2. max_risk_per_lot = max_risk_per_lot (strategy max_risk × lot_size)
  3. raw_lots = floor(risk_budget / max_risk_per_lot)
  4. cap by fno_sizing_max_position_pct of capital
  5. In high-VIX regime, halve the position (volatility scaling)
  6. Minimum 1 lot; 0 if strategy cost exceeds budget entirely

The sizer never allocates more than max_position_pct of capital to a single trade.
"""
from __future__ import annotations

import math
from decimal import Decimal


def compute_lots(
    portfolio_capital: Decimal,
    max_risk_per_lot: Decimal,
    lot_size: int,
    atm_premium: Decimal,
    *,
    risk_per_trade_pct: float = 0.01,
    max_position_pct: float = 0.15,
    vix_regime: str = "neutral",
) -> int:
    """Return the number of lots to buy/sell for a single strategy leg.

    Args:
        portfolio_capital: total paper-trading capital (Decimal, in ₹)
        max_risk_per_lot: maximum loss per lot (strategy max_risk)
        lot_size: contracts per lot for this instrument
        atm_premium: current ATM option premium (₹ per share)
        risk_per_trade_pct: fraction of capital to risk per trade (0.01 = 1%)
        max_position_pct: max fraction of capital in any single position (0.15 = 15%)
        vix_regime: "low" | "neutral" | "high"; "high" halves the position
    """
    if portfolio_capital <= 0 or max_risk_per_lot <= 0:
        return 0

    risk_budget = portfolio_capital * Decimal(str(risk_per_trade_pct))
    raw_lots = int(risk_budget / max_risk_per_lot)

    # Cap by max position size in nominal premium terms
    premium_per_lot = atm_premium * lot_size if atm_premium > 0 and lot_size > 0 else Decimal("0")
    max_lots_by_capital = 0
    if premium_per_lot > 0:
        max_lots_by_capital = int(
            portfolio_capital * Decimal(str(max_position_pct)) / premium_per_lot
        )
        raw_lots = min(raw_lots, max_lots_by_capital)

    # VIX volatility scaling
    if vix_regime == "high":
        raw_lots = max(raw_lots // 2, 0)

    if raw_lots >= 1:
        return raw_lots

    # Paper-trading floor: if the budget can't size by risk-per-trade but the
    # position still fits within max_position_pct of capital, allow exactly
    # 1 lot. This prevents the sizer from silently dropping every PROCEED
    # when a single ATM premium exceeds the per-trade risk budget on a
    # small account.
    if max_lots_by_capital >= 1:
        return 1
    return 0


def compute_stop_loss(
    entry_price: Decimal,
    strategy_name: str,
    *,
    hard_stop_pct: float = 0.50,
) -> Decimal:
    """Return the stop-loss premium level for a long option position.

    For long options: stop at hard_stop_pct below entry premium.
    (e.g., stop_loss = 50% of premium → exit if option loses half its value)
    """
    stop = entry_price * Decimal(str(1.0 - hard_stop_pct))
    return max(stop, Decimal("0.05"))


def compute_target(
    entry_price: Decimal,
    strategy_name: str,
    iv_regime: str,
    *,
    target_multiplier: float = 2.0,
) -> Decimal:
    """Return the profit-target premium for a long option.

    In high-IV regimes, reduce target to capture premium before crush.
    In low-IV regimes, let winners run with full multiplier.
    """
    if iv_regime == "high":
        target_multiplier = min(target_multiplier, 1.5)
    return entry_price * Decimal(str(target_multiplier))
