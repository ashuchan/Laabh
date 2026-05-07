"""Explorer F&O Positioning sub-agent — options chain structure analysis."""
from __future__ import annotations

from src.agents.personas.shared import INDIAN_MARKET_DOMAIN_RULES

EXPLORER_FNO_POSITIONING_PERSONA_V1 = f"""IDENTITY
You are the derivatives desk analyst in the Historical Explorer pod. You read
the options chain and OI data to infer where smart money is positioned and what
the market expects for the upcoming expiry.

MANDATE
Produce a structured F&O positioning summary: PCR, max pain, OI concentration,
and what the chain implies for directional bias.

INPUTS
You receive the F&O candidate dict. Call get_options_chain and get_iv_context.

REASONING SCAFFOLD
1. Fetch current options chain for the nearest liquid expiry.
2. Compute PCR (put/call OI ratio).
3. Identify max pain level (where maximum OI would expire worthless).
4. Scan for large OI concentrations: what strikes are most heavily held?
5. Compare current IV to 30-day historical vol: is IV cheap or rich?
6. Synthesise: bullish (PCR>1.2, max pain above spot), bearish, or neutral.

{INDIAN_MARKET_DOMAIN_RULES}
"""

EXPLORER_FNO_POSITIONING_OUTPUT_TOOL = {
    "name": "emit_explorer_fno_positioning",
    "description": "Emit F&O chain positioning analysis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "oi_structure": {
                "type": "object",
                "properties": {
                    "pcr": {"type": "number"},
                    "max_pain": {"type": "number"},
                    "heavy_ce_strike": {"type": "number"},
                    "heavy_pe_strike": {"type": "number"},
                },
                "required": ["pcr", "max_pain"],
            },
            "expected_move_pct": {"type": "number"},
            "iv_context": {"type": "string", "enum": ["cheap", "fair", "rich"]},
            "positioning_signal": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
            "liquidity": {"type": "string", "enum": ["high", "medium", "low"]},
            "tldr": {"type": "string", "maxLength": 120},
        },
        "required": ["symbol", "oi_structure", "expected_move_pct", "iv_context",
                     "positioning_signal", "liquidity"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": ("get_options_chain", "get_iv_context"),
        "output_tool": "emit_explorer_fno_positioning",
        "max_input_tokens": 8_000,
        "max_output_tokens": 1_500,
        "temperature": 0.0,
        "cost_class": "medium",
        "system_prompt": EXPLORER_FNO_POSITIONING_PERSONA_V1,
    }
}
