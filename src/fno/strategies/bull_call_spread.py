"""Bull Call Spread — buy ATM CE, sell OTM CE. Capped gain, reduced cost."""
from __future__ import annotations

from decimal import Decimal

from src.fno.strategies.base import BaseStrategy, Leg, StrategyRecommendation


class BullCallSpreadStrategy(BaseStrategy):
    name = "bull_call_spread"

    def is_applicable(self, direction: str, iv_regime: str, expiry_days: int) -> bool:
        return direction == "bullish" and expiry_days >= 2

    def select(
        self,
        direction: str,
        underlying_price: Decimal,
        iv_rank: float,
        iv_regime: str,
        expiry_days: int,
        chain_strikes: list[Decimal],
        atm_premium: Decimal,
    ) -> StrategyRecommendation | None:
        if not self.is_applicable(direction, iv_regime, expiry_days):
            return None
        atm = self._atm_strike(underlying_price, chain_strikes)
        otm = self._otm_strike(underlying_price, chain_strikes, "bullish", delta_offset=1)
        if atm is None or otm is None or atm == otm:
            return None
        width = otm - atm
        net_debit = atm_premium * Decimal("0.55")  # heuristic: OTM sells for ~45% of ATM
        return StrategyRecommendation(
            strategy_name=self.name,
            legs=[
                Leg(option_type="CE", strike=atm, action="BUY"),
                Leg(option_type="CE", strike=otm, action="SELL"),
            ],
            max_risk=net_debit,
            max_reward=width - net_debit,
            breakevens=[atm + net_debit],
            notes=f"Buy CE@{atm}, sell CE@{otm}; high-IV debit spread",
        )
