"""Explorer Trend sub-agent — technical price action analysis."""
from __future__ import annotations

EXPLORER_TREND_PERSONA_V1 = """IDENTITY
You are the technical analyst in the Historical Explorer pod. You analyse price
action and volume patterns for one instrument across multiple timeframes to
identify the dominant trend and tradable pattern.

MANDATE
For the given instrument and date range, identify the current trend regime,
key support/resistance levels, and whether there is a tradable chart pattern.

INPUTS
You receive the candidate dict from the Brain Triage output. Call get_price_aggregates
to fetch the OHLCV data you need.

REASONING SCAFFOLD
1. Fetch 60-day daily OHLCV and 20-day hourly OHLCV via get_price_aggregates.
2. Identify trend on daily: above/below 20-EMA, 50-EMA, 200-EMA.
3. Identify RSI(14) regime: overbought >70, oversold <30, neutral.
4. Check volume: is breakout/breakdown confirmed by volume?
5. Identify the single most tradable pattern (if any): flag, cup, breakout, etc.
6. Assess vs benchmark (Nifty 50 or sector index) for relative strength.
"""

EXPLORER_TREND_OUTPUT_TOOL = {
    "name": "emit_explorer_trend",
    "description": "Emit trend analysis for one instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "horizon_views": {
                "type": "object",
                "properties": {
                    "daily_trend": {"type": "string", "enum": ["bullish", "bearish", "sideways"]},
                    "hourly_trend": {"type": "string", "enum": ["bullish", "bearish", "sideways"]},
                    "key_support": {"type": "number"},
                    "key_resistance": {"type": "number"},
                    "rsi_14": {"type": "number"},
                },
                "required": ["daily_trend", "hourly_trend"],
            },
            "tradable_pattern": {"type": ["string", "null"], "maxLength": 100},
            "volume_confirmation": {"type": "boolean"},
            "regime_break": {"type": "boolean"},
            "vs_benchmark": {"type": "string", "enum": ["outperforming", "in_line", "underperforming"]},
            "tldr": {"type": "string", "maxLength": 120},
        },
        "required": ["symbol", "horizon_views", "vs_benchmark", "tldr"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": ("get_price_aggregates",),
        "output_tool": "emit_explorer_trend",
        "max_input_tokens": 6_000,
        "max_output_tokens": 1_500,
        "temperature": 0.0,
        "cost_class": "medium",
        "system_prompt": EXPLORER_TREND_PERSONA_V1,
    }
}
