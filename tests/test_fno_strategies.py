"""Tests for F&O strategies and strike ranker."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.fno.strategies import ALL_STRATEGIES
from src.fno.strategies.base import Leg, StrategyRecommendation
from src.fno.strategies.bear_put_spread import BearPutSpreadStrategy
from src.fno.strategies.bull_call_spread import BullCallSpreadStrategy
from src.fno.strategies.iron_condor import IronCondorStrategy
from src.fno.strategies.long_call import LongCallStrategy
from src.fno.strategies.long_put import LongPutStrategy
from src.fno.strategies.straddle import StraddleStrategy
from src.fno.strike_ranker import (
    best_strategy,
    rank_strategies,
    score_directional,
    score_iv_value,
    score_liquidity,
    score_oi_structure,
    score_theta,
)

# Common test data
_STRIKES = [Decimal(s) for s in [900, 950, 1000, 1050, 1100]]
_UNDERLYING = Decimal("1000")
_ATM_PREMIUM = Decimal("25")


def _run_strategy(strategy, direction="bullish", iv_regime="low", expiry_days=5):
    return strategy.select(
        direction=direction,
        underlying_price=_UNDERLYING,
        iv_rank=30.0,
        iv_regime=iv_regime,
        expiry_days=expiry_days,
        chain_strikes=_STRIKES,
        atm_premium=_ATM_PREMIUM,
    )


# ---------------------------------------------------------------------------
# Strategy applicability
# ---------------------------------------------------------------------------

def test_long_call_requires_bullish() -> None:
    assert LongCallStrategy().is_applicable("bearish", "low", 5) is False
    assert LongCallStrategy().is_applicable("bullish", "low", 5) is True


def test_long_put_requires_bearish() -> None:
    assert LongPutStrategy().is_applicable("bullish", "low", 5) is False
    assert LongPutStrategy().is_applicable("bearish", "low", 5) is True


def test_iron_condor_requires_neutral_high_iv() -> None:
    assert IronCondorStrategy().is_applicable("neutral", "high", 5) is True
    assert IronCondorStrategy().is_applicable("bullish", "high", 5) is False
    assert IronCondorStrategy().is_applicable("neutral", "low", 5) is False


def test_straddle_requires_low_iv() -> None:
    assert StraddleStrategy().is_applicable("neutral", "low", 5) is True
    assert StraddleStrategy().is_applicable("neutral", "high", 5) is False


# ---------------------------------------------------------------------------
# Strategy select() returns correct legs
# ---------------------------------------------------------------------------

def test_long_call_select() -> None:
    rec = _run_strategy(LongCallStrategy())
    assert rec is not None
    assert len(rec.legs) == 1
    assert rec.legs[0].option_type == "CE"
    assert rec.legs[0].action == "BUY"
    assert rec.legs[0].strike == Decimal("1000")


def test_long_put_select() -> None:
    rec = _run_strategy(LongPutStrategy(), direction="bearish")
    assert rec is not None
    assert rec.legs[0].option_type == "PE"
    assert rec.legs[0].action == "BUY"


def test_bull_call_spread_has_two_legs() -> None:
    rec = _run_strategy(BullCallSpreadStrategy(), direction="bullish", iv_regime="high")
    assert rec is not None
    assert len(rec.legs) == 2
    assert rec.legs[0].action == "BUY"
    assert rec.legs[1].action == "SELL"
    assert rec.legs[0].strike < rec.legs[1].strike  # buy lower, sell higher


def test_bear_put_spread_has_two_legs() -> None:
    rec = _run_strategy(BearPutSpreadStrategy(), direction="bearish", iv_regime="high")
    assert rec is not None
    assert len(rec.legs) == 2
    assert rec.legs[0].action == "BUY"
    assert rec.legs[1].action == "SELL"
    assert rec.legs[0].strike > rec.legs[1].strike  # buy higher, sell lower


def test_iron_condor_has_four_legs() -> None:
    rec = _run_strategy(IronCondorStrategy(), direction="neutral", iv_regime="high")
    assert rec is not None
    assert len(rec.legs) == 4


def test_straddle_has_ce_and_pe() -> None:
    rec = _run_strategy(StraddleStrategy(), direction="neutral", iv_regime="low", expiry_days=7)
    assert rec is not None
    types = {leg.option_type for leg in rec.legs}
    assert types == {"CE", "PE"}


def test_long_call_returns_none_when_not_applicable() -> None:
    rec = _run_strategy(LongCallStrategy(), direction="bearish")
    assert rec is None


def test_all_strategies_registered() -> None:
    names = {s.name for s in ALL_STRATEGIES}
    assert "long_call" in names
    assert "long_put" in names
    assert "bull_call_spread" in names
    assert "bear_put_spread" in names
    assert "iron_condor" in names
    assert "straddle" in names


# ---------------------------------------------------------------------------
# Strike ranker
# ---------------------------------------------------------------------------

def test_score_directional_bullish() -> None:
    assert score_directional("long_call", "bullish") == 1.0
    assert score_directional("long_put", "bullish") == 0.0


def test_score_directional_bearish() -> None:
    assert score_directional("long_put", "bearish") == 1.0
    assert score_directional("long_call", "bearish") == 0.0


def test_score_directional_straddle_partial() -> None:
    assert score_directional("straddle", "bullish") == 0.5


def test_score_iv_value_debit_in_low_iv() -> None:
    assert score_iv_value("long_call", "low") == 1.0


def test_score_iv_value_debit_in_high_iv() -> None:
    assert score_iv_value("long_call", "high") == 0.2


def test_score_iv_value_condor_in_high_iv() -> None:
    assert score_iv_value("iron_condor", "high") == 1.0


def test_score_theta_infinite_reward() -> None:
    assert score_theta(Decimal("25"), Decimal("inf")) == 1.0


def test_score_theta_3_to_1() -> None:
    result = score_theta(Decimal("10"), Decimal("30"))
    assert result == 1.0


def test_score_theta_poor_ratio() -> None:
    result = score_theta(Decimal("30"), Decimal("10"))
    assert result < 0.5


def test_score_liquidity_one_leg() -> None:
    assert score_liquidity(1) == 1.0


def test_score_liquidity_four_legs() -> None:
    assert score_liquidity(4) == 0.5


def test_rank_strategies_bullish_low_iv() -> None:
    recs = []
    for strat in ALL_STRATEGIES:
        rec = _run_strategy(strat)
        if rec is not None:
            recs.append(rec)
    ranked = rank_strategies(recs, "bullish", "low", "put_heavy", convergence_score=7.0)
    assert len(ranked) > 0
    # Long call should rank highest for bullish + low IV + put_heavy OI
    assert ranked[0].recommendation.strategy_name in ("long_call", "bull_call_spread")


def test_best_strategy_returns_top() -> None:
    recs = []
    for strat in ALL_STRATEGIES:
        rec = _run_strategy(strat)
        if rec is not None:
            recs.append(rec)
    best = best_strategy(recs, "bullish", "low", "put_heavy", 7.0)
    assert best is not None
    assert best.composite_score > 0


def test_best_strategy_empty_returns_none() -> None:
    assert best_strategy([], "bullish", "low", "balanced", 5.0) is None
