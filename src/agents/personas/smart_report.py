"""Smart-report persona — terse intraday status for ONE position or watch symbol.

Runs once per name in the midday review. Cheap Haiku call. The output is
designed to be CEO-readable in 5 seconds: P&L vs morning, key intraday
metric, kill-switch state, and a single action recommendation.

Each smart_report is later consumed by the midday_ceo persona to make the
midday capital call.
"""
from __future__ import annotations

from src.agents.personas.shared import INTRADAY_BRIEF

SMART_REPORT_PERSONA_V1 = f"""IDENTITY
You are an intraday status compiler. For ONE position or watch-symbol you
synthesise the morning thesis + intraday inputs into a 60-second briefing
for the CEO.

{INTRADAY_BRIEF}

INPUTS
- symbol, asset_class
- morning_thesis (what the equity/F&O expert said)
- intraday_trend (trend sub-agent's output for this symbol)
- intraday_sentiment (sentiment-drift output)
- intraday_news (recent search_raw_content results, ≤ 3 items)
- live_quote (last price, day-change %, volume vs 20d avg)
- kill_switches (the morning-defined triggers for this symbol)

REASONING SCAFFOLD
1. Compare current price vs morning entry zone / target / stop.
2. Compare intraday sentiment delta vs morning. >0.1 = material shift.
3. Has any kill_switch trigger fired?
4. Pick ONE of: stay, scale_down, exit, watch_for_add.
5. Keep rationale ≤ 80 tokens.
"""

SMART_REPORT_OUTPUT_TOOL = {
    "name": "emit_smart_report",
    "description": "Emit a midday smart report for one position or watch-symbol.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "asset_class": {"type": "string", "enum": ["fno", "equity", "cash"]},
            "as_of": {"type": "string"},
            "since_morning": {
                "type": "object",
                "properties": {
                    "price_change_pct": {"type": ["number", "null"]},
                    "sentiment_delta": {"type": ["number", "null"]},
                    "volume_vs_20d_avg": {"type": ["number", "null"]},
                },
            },
            "kill_switch_armed": {"type": "boolean",
                                   "description": "True if any morning kill_switch is now within 1% of trigger."},
            "regime_change": {"type": "boolean",
                              "description": "True if intraday trend reversed vs morning."},
            "fresh_signals": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string"},
                "description": "Brief one-liners on new signals seen in the last 3-4 hours.",
            },
            "recommendation": {
                "type": "string",
                "enum": ["stay", "scale_down", "exit", "watch_for_add"],
            },
            "rationale": {"type": "string", "maxLength": 240},
            "tldr": {"type": "string", "maxLength": 100},
        },
        "required": ["symbol", "as_of", "kill_switch_armed", "regime_change",
                     "recommendation", "rationale", "tldr"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-haiku-4-5-20251001",
        "fallback_model": None,
        "tools": ("search_raw_content", "get_price_aggregates", "get_sentiment_history"),
        "output_tool": "emit_smart_report",
        "max_input_tokens": 6_000,
        "max_output_tokens": 800,
        "temperature": 0.0,
        "cost_class": "cheap",
        "system_prompt": SMART_REPORT_PERSONA_V1,
    }
}
