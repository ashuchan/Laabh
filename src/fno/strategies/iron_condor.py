"""Iron Condor — sell ATM±1 strangle, buy further OTM wings. High-IV neutral."""
from __future__ import annotations

from decimal import Decimal

from src.fno.strategies.base import BaseStrategy, Leg, StrategyRecommendation


class IronCondorStrategy(BaseStrategy):
    name = "iron_condor"

    def is_applicable(self, direction: str, iv_regime: str, expiry_days: int) -> bool:
        return direction == "neutral" and iv_regime == "high" and expiry_days >= 3

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
        if atm is None:
            return None
        otm_call = self._otm_strike(underlying_price, chain_strikes, "bullish", 1)
        wing_call = self._otm_strike(underlying_price, chain_strikes, "bullish", 2)
        otm_put = self._otm_strike(underlying_price, chain_strikes, "bearish", 1)
        wing_put = self._otm_strike(underlying_price, chain_strikes, "bearish", 2)
        if not all([otm_call, wing_call, otm_put, wing_put]):
            return None
        net_credit = atm_premium * Decimal("0.30")  # heuristic
        call_width = wing_call - otm_call
        put_width = otm_put - wing_put
        max_risk = min(call_width, put_width) - net_credit
        return StrategyRecommendation(
            strategy_name=self.name,
            legs=[
                Leg(option_type="CE", strike=otm_call, action="SELL"),
                Leg(option_type="CE", strike=wing_call, action="BUY"),
                Leg(option_type="PE", strike=otm_put, action="SELL"),
                Leg(option_type="PE", strike=wing_put, action="BUY"),
            ],
            max_risk=max_risk,
            max_reward=net_credit,
            breakevens=[otm_call + net_credit, otm_put - net_credit],
            notes="High-IV neutral; range-bound expected",
        )
