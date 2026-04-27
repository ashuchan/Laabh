"""Long Straddle — buy ATM CE + PE. Profits from large moves in either direction."""
from __future__ import annotations

from decimal import Decimal

from src.fno.strategies.base import BaseStrategy, Leg, StrategyRecommendation


class StraddleStrategy(BaseStrategy):
    name = "straddle"

    def is_applicable(self, direction: str, iv_regime: str, expiry_days: int) -> bool:
        # Use when direction is uncertain but high volatility is expected
        return direction in ("neutral", "bullish", "bearish") and iv_regime == "low" and expiry_days >= 5

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
        total_debit = atm_premium * Decimal("2")
        return StrategyRecommendation(
            strategy_name=self.name,
            legs=[
                Leg(option_type="CE", strike=strike, action="BUY"),
                Leg(option_type="PE", strike=strike, action="BUY"),
            ],
            max_risk=total_debit,
            max_reward=Decimal("inf"),
            breakevens=[strike - total_debit, strike + total_debit],
            notes=f"ATM straddle at {strike}; low-IV long vol play",
        )
