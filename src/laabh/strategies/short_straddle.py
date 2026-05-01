"""
Short Straddle for Laabh — adapted from algo_trading_strategies_india (MIT).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.integrations.openalgo.client import place_paper_order


@dataclass
class ShortStraddleConfig:
    underlying: str
    expiry: str
    atm_strike: int
    lot_size: int
    trailing_stop_pct: float = 0.25   # trail at 25% from peak P&L
    daily_target_pct: float = 0.30    # exit at 30% of max credit
    discipline_mode: bool = True      # block re-entry after stop hit


class ShortStraddle:
    """
    Structure: Sell ATM CE + Sell ATM PE.
    High-premium strategy. Managed with trailing stop on combined premium.
    """

    def __init__(self, cfg: ShortStraddleConfig) -> None:
        self.cfg = cfg
        self.peak_pnl: float = 0.0
        self.stop_triggered: bool = False
        self.cooldown_until: str | None = None

    def enter(self) -> dict:
        """Place both legs via OpenAlgo paper trading sandbox."""
        c = self.cfg
        results = [
            place_paper_order(
                symbol=f"{c.underlying}{c.expiry}{c.atm_strike}CE",
                exchange="NFO",
                action="SELL",
                quantity=c.lot_size,
            ),
            place_paper_order(
                symbol=f"{c.underlying}{c.expiry}{c.atm_strike}PE",
                exchange="NFO",
                action="SELL",
                quantity=c.lot_size,
            ),
        ]
        return {"status": "entered", "legs": results}

    def update_trailing_stop(self, current_pnl: float) -> str | None:
        """Track peak P&L and trigger trailing stop when drawdown exceeds threshold."""
        if current_pnl > self.peak_pnl:
            self.peak_pnl = current_pnl

        trailing_stop_level = self.peak_pnl * (1 - self.cfg.trailing_stop_pct)
        if current_pnl < trailing_stop_level and self.peak_pnl > 0:
            if self.cfg.discipline_mode:
                self.stop_triggered = True
            return "TRAILING_STOP"
        return None
