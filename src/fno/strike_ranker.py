"""Strike ranker — scores and ranks strategy recommendations for a candidate.

Given a set of StrategyRecommendation objects (from multiple strategies),
the ranker assigns a composite score to each and picks the best one.

Scoring dimensions (weights from config):
  - directional_score  (0-1): how well the strategy aligns with direction
  - convergence_score  (0-1): reuses Phase-2 convergence score (normalised)
  - iv_value_score     (0-1): buying in low IV, selling in high IV
  - theta_score        (0-1): risk/reward; low max_risk relative to max_reward
  - oi_structure_score (0-1): strategy matches OI structure (put_heavy=bullish support)
  - liquidity_score    (0-1): fewer legs = more liquid execution
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from src.fno.strategies.base import StrategyRecommendation


@dataclass
class RankedStrategy:
    recommendation: StrategyRecommendation
    directional_score: float = 0.0
    iv_value_score: float = 0.0
    theta_score: float = 0.0
    oi_structure_score: float = 0.0
    liquidity_score: float = 0.0
    composite_score: float = 0.0


# ---------------------------------------------------------------------------
# Individual dimension scorers (pure, unit-testable)
# ---------------------------------------------------------------------------

def score_directional(strategy_name: str, direction: str) -> float:
    """1.0 if the strategy naturally profits from the given direction."""
    bullish_strategies = {"long_call", "bull_call_spread"}
    bearish_strategies = {"long_put", "bear_put_spread"}
    neutral_strategies = {"iron_condor", "straddle"}

    if direction == "bullish" and strategy_name in bullish_strategies:
        return 1.0
    if direction == "bearish" and strategy_name in bearish_strategies:
        return 1.0
    if direction == "neutral" and strategy_name in neutral_strategies:
        return 1.0
    # Partial credit for straddle when directional (vol play still valid)
    if strategy_name == "straddle":
        return 0.5
    return 0.0


def score_iv_value(strategy_name: str, iv_regime: str) -> float:
    """Reward debit strategies in low IV, credit strategies in high IV."""
    debit_strategies = {"long_call", "long_put", "bull_call_spread", "bear_put_spread", "straddle"}
    credit_strategies = {"iron_condor"}

    if strategy_name in debit_strategies and iv_regime == "low":
        return 1.0
    if strategy_name in debit_strategies and iv_regime == "neutral":
        return 0.6
    if strategy_name in credit_strategies and iv_regime == "high":
        return 1.0
    if strategy_name in credit_strategies and iv_regime == "neutral":
        return 0.5
    return 0.2  # wrong IV regime


def score_theta(max_risk: Decimal, max_reward: Decimal) -> float:
    """Reward/risk ratio normalised to 0-1. Caps at ratio=3 → score=1."""
    if max_risk <= 0:
        return 0.5
    if max_reward == Decimal("inf"):
        return 1.0
    ratio = float(max_reward / max_risk)
    return min(ratio / 3.0, 1.0)


def score_oi_structure(strategy_name: str, oi_structure: str) -> float:
    """Bonus when OI wall aligns with strategy direction."""
    if oi_structure == "put_heavy" and strategy_name in ("long_call", "bull_call_spread"):
        return 1.0   # Put OI = support = bullish confirmation
    if oi_structure == "call_heavy" and strategy_name in ("long_put", "bear_put_spread"):
        return 1.0   # Call OI = resistance = bearish confirmation
    if oi_structure == "balanced" and strategy_name in ("iron_condor", "straddle"):
        return 1.0
    return 0.5       # neutral


def score_liquidity(n_legs: int) -> float:
    """Fewer legs = easier to fill. Score: 1 leg=1.0, 2=0.75, 4=0.5."""
    return {1: 1.0, 2: 0.75, 3: 0.6, 4: 0.5}.get(n_legs, 0.3)


# ---------------------------------------------------------------------------
# Composite ranking
# ---------------------------------------------------------------------------

def rank_strategies(
    recommendations: Sequence[StrategyRecommendation],
    direction: str,
    iv_regime: str,
    oi_structure: str,
    convergence_score: float,  # 0-10, will be normalised to 0-1
    *,
    w_directional: float = 0.30,
    w_convergence: float = 0.20,
    w_iv_value: float = 0.15,
    w_theta: float = 0.10,
    w_oi_structure: float = 0.15,
    w_liquidity: float = 0.10,
) -> list[RankedStrategy]:
    """Score and sort all strategy recommendations. Best first."""
    conv_norm = min(convergence_score / 10.0, 1.0)
    ranked: list[RankedStrategy] = []

    for rec in recommendations:
        d_score = score_directional(rec.strategy_name, direction)
        iv_score = score_iv_value(rec.strategy_name, iv_regime)
        t_score = score_theta(rec.max_risk, rec.max_reward)
        oi_score = score_oi_structure(rec.strategy_name, oi_structure)
        liq_score = score_liquidity(len(rec.legs))

        composite = (
            d_score * w_directional
            + conv_norm * w_convergence
            + iv_score * w_iv_value
            + t_score * w_theta
            + oi_score * w_oi_structure
            + liq_score * w_liquidity
        )
        composite = round(composite, 4)
        rec.score = composite

        ranked.append(RankedStrategy(
            recommendation=rec,
            directional_score=d_score,
            iv_value_score=iv_score,
            theta_score=t_score,
            oi_structure_score=oi_score,
            liquidity_score=liq_score,
            composite_score=composite,
        ))

    ranked.sort(key=lambda r: r.composite_score, reverse=True)
    return ranked


def best_strategy(
    recommendations: Sequence[StrategyRecommendation],
    direction: str,
    iv_regime: str,
    oi_structure: str,
    convergence_score: float,
    **weight_kwargs,
) -> RankedStrategy | None:
    """Return the highest-scoring strategy or None if list is empty."""
    ranked = rank_strategies(
        recommendations, direction, iv_regime, oi_structure, convergence_score,
        **weight_kwargs,
    )
    return ranked[0] if ranked else None
