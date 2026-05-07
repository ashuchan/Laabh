"""CEO Bear persona — builds the strongest case FOR caution / cash."""
from __future__ import annotations

from src.agents.personas.shared import INDIAN_MARKET_DOMAIN_RULES

CEO_BEAR_PERSONA_V1 = f"""IDENTITY
You are the bearish portfolio manager in the CEO debate. Your job is to build
the strongest possible case FOR caution — holding cash, reducing size, or
refusing today's trades. You are a devil's advocate for the bear case — you
will challenge the bull's optimism with specific, provenance-rich arguments.

MANDATE
Produce the strongest rational bear argument against today's proposed trades.
Back every claim with specific provenance. Include the 3 most compelling
counter-evidence pieces and 3 bull rebuttals with your responses.

INPUTS
You receive the exact same data packet as the Bull (cached — only your system
prompt differs). Call get_full_rationale if you need supporting detail.

REASONING SCAFFOLD
1. Identify the 3 strongest bear signals or risk factors.
2. For each: state evidence type and provenance.
3. Pre-empt the 3 most likely bull counter-arguments.
4. Propose an allocation biased toward caution / defined-risk.
5. Identify what specific events/prints would make you abandon the bear case.
6. Self-check: are you being bears for the sake of it, or do you have genuine evidence?
{INDIAN_MARKET_DOMAIN_RULES}"""

CEO_BEAR_OUTPUT_TOOL = {
    "name": "emit_ceo_bear",
    "description": "Emit the bearish portfolio manager argument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "stance": {
                "type": "string",
                "enum": ["bullish_aggressive", "bullish_measured", "bearish_measured", "bearish_defensive"],
            },
            "core_thesis": {"type": "string", "maxLength": 400},
            "top_3_evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "evidence_type": {
                            "type": "string",
                            "enum": ["signal", "filing", "technical", "macro", "positioning"],
                        },
                        "provenance": {"type": "object"},
                        "weight": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["claim", "evidence_type", "weight"],
                },
            },
            "top_3_counter_to_other_side": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "likely_other_side_claim": {"type": "string"},
                        "rebuttal": {"type": "string"},
                        "rebuttal_strength": {"type": "string", "enum": ["weak", "medium", "strong"]},
                    },
                    "required": ["likely_other_side_claim", "rebuttal", "rebuttal_strength"],
                },
            },
            "preferred_allocation": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "asset_class": {"type": "string", "enum": ["fno", "equity", "cash"]},
                        "underlying_or_symbol": {"type": "string"},
                        "capital_pct": {"type": "number"},
                    },
                    "required": ["asset_class", "capital_pct"],
                },
            },
            "conviction": {"type": "number", "minimum": 0, "maximum": 1},
            "what_would_change_my_mind": {
                "type": "array",
                "minItems": 3,
                "maxItems": 5,
                "items": {"type": "string"},
            },
        },
        "required": ["stance", "core_thesis", "top_3_evidence", "preferred_allocation",
                     "conviction", "what_would_change_my_mind"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-opus-4-7",
        "fallback_model": "claude-sonnet-4-6",
        "tools": ("get_full_rationale",),
        "output_tool": "emit_ceo_bear",
        "max_input_tokens": 18_000,
        "max_output_tokens": 3_000,
        "temperature": 0.1,
        "cost_class": "expensive",
        "stream_response": True,
        "system_prompt": CEO_BEAR_PERSONA_V1,
    }
}
