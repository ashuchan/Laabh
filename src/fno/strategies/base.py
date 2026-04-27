"""Base class for F&O option strategies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


OptionType = Literal["CE", "PE"]
StrategyName = Literal[
    "long_call",
    "long_put",
    "bull_call_spread",
    "bear_put_spread",
    "iron_condor",
    "straddle",
]


@dataclass
class Leg:
    """One option contract within a strategy."""
    option_type: OptionType
    strike: Decimal
    action: Literal["BUY", "SELL"]
    quantity: int = 1  # in lots


@dataclass
class StrategyRecommendation:
    """Output of a strategy's select() method."""
    strategy_name: StrategyName
    legs: list[Leg]
    max_risk: Decimal          # max possible loss (positive value)
    max_reward: Decimal        # max possible gain (positive value, or Decimal("inf"))
    breakevens: list[Decimal]
    score: float = 0.0         # ranker score (set externally)
    notes: str = ""


class BaseStrategy(ABC):
    """Abstract base for all F&O strategies."""

    name: StrategyName

    @abstractmethod
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
        """Return a StrategyRecommendation or None if the strategy is not applicable."""

    def is_applicable(
        self,
        direction: str,
        iv_regime: str,
        expiry_days: int,
    ) -> bool:
        """Quick pre-check before running full select(). Override in subclasses."""
        return True

    @staticmethod
    def _atm_strike(underlying: Decimal, strikes: list[Decimal]) -> Decimal | None:
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(s - underlying))

    @staticmethod
    def _otm_strike(
        underlying: Decimal,
        strikes: list[Decimal],
        direction: str,
        delta_offset: int = 1,
    ) -> Decimal | None:
        """Return the Nth OTM strike above (call) or below (put) ATM."""
        atm = BaseStrategy._atm_strike(underlying, strikes)
        if atm is None:
            return None
        if direction == "bullish":
            otm_candidates = sorted(s for s in strikes if s > atm)
        else:
            otm_candidates = sorted((s for s in strikes if s < atm), reverse=True)
        if len(otm_candidates) < delta_offset:
            return otm_candidates[-1] if otm_candidates else None
        return otm_candidates[delta_offset - 1]
