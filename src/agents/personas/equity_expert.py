"""Equity Expert persona — cash equity trade thesis."""
from __future__ import annotations

from src.agents.personas.shared import INDIAN_MARKET_DOMAIN_RULES

EQUITY_EXPERT_PERSONA_V1 = f"""IDENTITY
You are the equity research analyst for the desk. For one equity candidate, you
produce a structured trade recommendation with entry zone, target, stop, and
position sizing based on the day's news, technicals, and fundamentals.

MANDATE
For the given equity candidate, produce ONE specific trade recommendation with
entry zone, target, stop, horizon, conviction, and the single most important
catalyst to monitor. Refuse if the setup is ambiguous or risk-reward is poor.

INPUTS
You receive the equity candidate enriched with News Finder, Editor, and Explorer
outputs. Call score_technicals, score_fundamentals, and position_sizing.

REASONING SCAFFOLD
1. Score technicals (trend, momentum, volume): call score_technicals.
2. Score fundamentals (P/E vs sector, earnings momentum): call score_fundamentals.
3. Check news sentiment from News Finder output: go/no-go from Editor.
4. Compute entry zone: tight range around current price given support/resistance.
5. Compute target: based on next resistance level or fundamental fair value.
6. Compute stop: below key support or where the thesis breaks.
7. Ensure R:R ≥ 2 (target - entry ≥ 2× entry - stop).
8. Size the position: call position_sizing.

{INDIAN_MARKET_DOMAIN_RULES}
"""

EQUITY_EXPERT_OUTPUT_TOOL = {
    "name": "emit_equity_expert",
    "description": "Emit the equity trade recommendation for one candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "decision": {"type": "string", "enum": ["BUY", "SELL", "HOLD", "REFUSE"]},
            "thesis": {"type": "string", "maxLength": 500},
            "entry_zone": {
                "type": "object",
                "properties": {
                    "low": {"type": "number"},
                    "high": {"type": "number"},
                },
                "required": ["low", "high"],
            },
            "target": {"type": "number"},
            "stop": {"type": "number"},
            "horizon": {"type": "string", "enum": ["intraday", "1d", "3d", "5d", "10d", "swing"]},
            "conviction": {"type": "number", "minimum": 0, "maximum": 1},
            "expected_pnl_pct": {"type": "number"},
            "max_loss_pct": {"type": "number"},
            "capital_pct": {"type": "number"},
            "catalyst_to_monitor": {"type": "string", "maxLength": 200},
            "refused": {"type": "boolean"},
            "refuse_reason": {"type": ["string", "null"]},
        },
        "required": ["symbol", "decision", "thesis", "conviction", "refused"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": ("score_technicals", "score_fundamentals", "position_sizing"),
        "output_tool": "emit_equity_expert",
        "max_input_tokens": 10_000,
        "max_output_tokens": 2_000,
        "temperature": 0.1,
        "cost_class": "medium",
        "system_prompt": EQUITY_EXPERT_PERSONA_V1,
    }
}
