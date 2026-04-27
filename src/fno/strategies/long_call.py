"""Long Call strategy — buy ATM CE when direction is bullish and IV rank is low."""
from __future__ import annotations

from decimal import Decimal

from src.fno.strategies.base import BaseStrategy, Leg, StrategyRecommendation


class LongCallStrategy(BaseStrategy):
    name = "long_call"

    def is_applicable(self, direction: str, iv_regime: str, expiry_days: int) -> bool:
        return direction == "bullish" and iv_regime != "high" and expiry_days >= 2

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
        strike = self._atm_strike(underlying_price, chain_strikes)
        if strike is None:
            return None
        return StrategyRecommendation(
            strategy_name=self.name,
            legs=[Leg(option_type="CE", strike=strike, action="BUY")],
            max_risk=atm_premium,
            max_reward=Decimal("inf"),
            breakevens=[strike + atm_premium],
            notes=f"ATM CE at {strike}; low-IV long premium play",
        )
