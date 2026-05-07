"""Explorer Sentiment Drift sub-agent — tracks how sentiment has evolved."""
from __future__ import annotations

from src.agents.personas.shared import INDIAN_MARKET_DOMAIN_RULES

EXPLORER_SENTIMENT_DRIFT_PERSONA_V1 = f"""IDENTITY
You are the sentiment historian in the Historical Explorer pod. You track how market
sentiment toward one instrument has shifted over the past 30 days, identify
divergences between price and sentiment, and flag regime shifts.

MANDATE
Produce a structured sentiment drift analysis showing whether the crowd is
becoming more bullish or bearish, and whether that drift is in line with price.

INPUTS
You receive the candidate dict. Call get_sentiment_history to fetch time-series data.

REASONING SCAFFOLD
1. Fetch daily sentiment scores for the instrument over 30 days.
2. Compute 7-day, 14-day, 30-day rolling averages.
3. Compare today's sentiment vs 30-day baseline: is the crowd warming or cooling?
4. Check price vs sentiment divergence: price up but sentiment falling = distribution?
5. Identify the phase: early_bull, late_bull, early_bear, late_bear, or neutral.
6. Flag a regime_shift if the sentiment crossed zero in the past 5 days.
{INDIAN_MARKET_DOMAIN_RULES}"""

EXPLORER_SENTIMENT_DRIFT_OUTPUT_TOOL = {
    "name": "emit_explorer_sentiment_drift",
    "description": "Emit sentiment drift analysis for one instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "sentiment_phase": {
                "type": "string",
                "enum": ["early_bull", "late_bull", "neutral", "early_bear", "late_bear"],
            },
            "today_vs_30d": {
                "type": "object",
                "properties": {
                    "today_score": {"type": "number"},
                    "avg_30d": {"type": "number"},
                    "delta": {"type": "number"},
                },
                "required": ["today_score", "avg_30d", "delta"],
            },
            "convergence_trend": {"type": "string", "enum": ["improving", "stable", "deteriorating"]},
            "price_sentiment_divergence": {
                "type": "boolean",
                "description": "True if price and sentiment are moving in opposite directions",
            },
            "regime_shift": {"type": "boolean"},
            "tldr": {"type": "string", "maxLength": 120},
        },
        "required": ["symbol", "sentiment_phase", "today_vs_30d", "convergence_trend",
                     "price_sentiment_divergence", "regime_shift"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": ("get_sentiment_history",),
        "output_tool": "emit_explorer_sentiment_drift",
        "max_input_tokens": 6_000,
        "max_output_tokens": 1_200,
        "temperature": 0.0,
        "cost_class": "medium",
        "system_prompt": EXPLORER_SENTIMENT_DRIFT_PERSONA_V1,
    }
}
