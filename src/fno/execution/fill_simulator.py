"""Fill simulator — models realistic option order fills for paper trading.

In paper trading there is no real exchange; we simulate fills using the
current bid-ask spread with a configurable slippage model.

Fill price logic (per leg):
  BUY  → fill at ask + (ask - bid) * slippage_factor
  SELL → fill at bid - (bid - ask) * slippage_factor  (always positive slippage cost)

For index options we assume brokerage is ₹0 (paper trading), but the
Securities Transaction Tax (STT) and Exchange Transaction Charge (ETC) are
modelled as configurable basis-points to keep P&L realistic.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

# Regulatory costs (approximate NSE F&O rates as of FY2025)
_STT_BPS_BUY = Decimal("0")        # STT on buy side is zero for options
_STT_BPS_SELL = Decimal("0.625")   # 0.00625% of premium on sell side
_ETC_BPS = Decimal("0.05")         # exchange transaction charge per side


@dataclass
class FillResult:
    """Result of simulating a fill for one leg."""
    action: Literal["BUY", "SELL"]
    fill_price: Decimal
    quantity_lots: int
    lot_size: int
    quantity_contracts: int          # lots × lot_size
    gross_premium: Decimal           # fill_price × contracts
    stt: Decimal
    etc: Decimal
    net_cost: Decimal                # positive = outflow (debit), negative = inflow (credit)


def simulate_fill(
    action: Literal["BUY", "SELL"],
    bid: Decimal,
    ask: Decimal,
    quantity_lots: int,
    lot_size: int,
    slippage_factor: Decimal = Decimal("0.10"),
) -> FillResult:
    """Simulate a single-leg option fill.

    slippage_factor: fraction of the bid-ask spread added as adverse slippage.
    Default 0.10 = 10% of spread (conservative for liquid options).
    """
    spread = ask - bid
    if action == "BUY":
        fill_price = ask + spread * slippage_factor
    else:
        fill_price = bid - spread * slippage_factor
        fill_price = max(fill_price, Decimal("0.05"))  # floor at tick size

    fill_price = fill_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    contracts = quantity_lots * lot_size
    gross = fill_price * contracts

    stt = _compute_stt(action, gross)
    etc = _compute_etc(gross)

    if action == "BUY":
        net_cost = gross + stt + etc          # outflow
    else:
        net_cost = -(gross - stt - etc)       # inflow (negative = cash received)

    return FillResult(
        action=action,
        fill_price=fill_price,
        quantity_lots=quantity_lots,
        lot_size=lot_size,
        quantity_contracts=contracts,
        gross_premium=gross,
        stt=stt,
        etc=etc,
        net_cost=net_cost,
    )


def _compute_stt(action: str, gross_premium: Decimal) -> Decimal:
    if action == "SELL":
        return (gross_premium * _STT_BPS_SELL / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    return Decimal("0")


def _compute_etc(gross_premium: Decimal) -> Decimal:
    return (gross_premium * _ETC_BPS / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def total_net_cost(fills: list[FillResult]) -> Decimal:
    """Sum net cost across all legs. Positive = net debit (cash out)."""
    return sum(f.net_cost for f in fills)
