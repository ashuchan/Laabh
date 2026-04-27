"""F&O strategy registry — all available option strategies."""
from __future__ import annotations

from src.fno.strategies.base import BaseStrategy, Leg, StrategyRecommendation
from src.fno.strategies.bear_put_spread import BearPutSpreadStrategy
from src.fno.strategies.bull_call_spread import BullCallSpreadStrategy
from src.fno.strategies.iron_condor import IronCondorStrategy
from src.fno.strategies.long_call import LongCallStrategy
from src.fno.strategies.long_put import LongPutStrategy
from src.fno.strategies.straddle import StraddleStrategy

ALL_STRATEGIES: list[BaseStrategy] = [
    LongCallStrategy(),
    LongPutStrategy(),
    BullCallSpreadStrategy(),
    BearPutSpreadStrategy(),
    IronCondorStrategy(),
    StraddleStrategy(),
]

__all__ = [
    "ALL_STRATEGIES",
    "BaseStrategy",
    "BearPutSpreadStrategy",
    "BullCallSpreadStrategy",
    "IronCondorStrategy",
    "Leg",
    "LongCallStrategy",
    "LongPutStrategy",
    "StrategyRecommendation",
    "StraddleStrategy",
]