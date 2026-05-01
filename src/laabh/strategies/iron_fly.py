"""
Iron Fly strategy for Laabh — adapted from algo_trading_strategies_india (MIT).
Adapted for: Laabh's signal pipeline, OpenAlgo order routing, Kafka alerts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.integrations.openalgo.client import place_paper_order


@dataclass
class IronFlyConfig:
    underlying: str          # e.g. "NIFTY"
    expiry: str              # e.g. "26JUN25"
    atm_strike: int          # resolved by strike ranker
    lot_size: int            # from OpenAlgo symbol master
    target_pct: float = 0.40  # 40% of max profit
    stop_pct: float = 1.00    # 100% of premium received (1:1)
    mtm_check_interval: int = 300  # seconds
    wing_width: int = 100         # OTM wing distance (points)


class IronFly:
    """
    Structure: Sell ATM CE + Sell ATM PE + Buy OTM CE + Buy OTM PE
    Net credit position. Profit if underlying stays near ATM at expiry.
    """

    def __init__(self, cfg: IronFlyConfig) -> None:
        self.cfg = cfg
        self.legs: list[dict] = []
        self.entry_premium: float = 0.0
        self.position_id: str = ""

    def enter(self) -> dict:
        """Place 4 legs via OpenAlgo paper trading sandbox."""
        c = self.cfg

        legs_params = [
            dict(
                symbol=f"{c.underlying}{c.expiry}{c.atm_strike}CE",
                exchange="NFO",
                action="SELL",
                quantity=c.lot_size,
            ),
            dict(
                symbol=f"{c.underlying}{c.expiry}{c.atm_strike}PE",
                exchange="NFO",
                action="SELL",
                quantity=c.lot_size,
            ),
            dict(
                symbol=f"{c.underlying}{c.expiry}{c.atm_strike + c.wing_width}CE",
                exchange="NFO",
                action="BUY",
                quantity=c.lot_size,
            ),
            dict(
                symbol=f"{c.underlying}{c.expiry}{c.atm_strike - c.wing_width}PE",
                exchange="NFO",
                action="BUY",
                quantity=c.lot_size,
            ),
        ]

        results = [place_paper_order(**leg) for leg in legs_params]
        self.legs = results
        return {"status": "entered", "legs": results}

    def check_mtm_exit(self, current_pnl: float) -> str | None:
        """
        Returns "TARGET"|"STOP"|None.
        MTM-based exit logic from algo_trading_strategies_india.
        """
        max_profit = self.entry_premium * self.cfg.lot_size
        target_pnl = max_profit * self.cfg.target_pct
        stop_pnl = -max_profit * self.cfg.stop_pct

        if current_pnl >= target_pnl:
            return "TARGET"
        if current_pnl <= stop_pnl:
            return "STOP"
        return None
