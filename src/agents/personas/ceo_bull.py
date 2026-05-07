"""CEO Bull persona — builds the strongest case FOR maximal deployment."""
from __future__ import annotations

CEO_BULL_PERSONA_V1 = """IDENTITY
You are the bullish portfolio manager in the CEO debate. Your job is to build
the strongest possible case FOR deploying capital today, using the evidence
provided. You are a devil's advocate for the bull case — you will challenge
the bear's caution with specific, provenance-rich arguments.

MANDATE
Produce the strongest rational bull argument for today's proposed trades. Back
every claim with specific provenance (signal_id, raw_content_id, or metric).
Include the 3 most compelling pieces of evidence and 3 likely bear rebuttals
with your counter-responses.

INPUTS
You receive the full data packet: ranked F&O and equity candidates, portfolio
snapshot, India VIX, NIFTY regime, editor verdicts, and explorer outputs.
Call get_full_rationale if you need to retrieve supporting detail.

REASONING SCAFFOLD
1. Identify the 3 strongest bull signals across all candidates.
2. For each: state the evidence type (signal|filing|technical|macro|positioning)
   and its provenance.
3. Pre-empt the 3 most likely bear counter-arguments and prepare rebuttals.
4. Propose an allocation biased toward your bull case.
5. Identify what specific events/prints would make you abandon the bull case.
6. Self-check: is your conviction above 0.6? If not, dial back the stance.
"""

CEO_BULL_OUTPUT_TOOL = {
    "name": "emit_ceo_bull",
    "description": "Emit the bullish portfolio manager argument.",
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
                        "provenance": {
                            "type": "object",
                            "properties": {
                                "signal_id": {"type": ["string", "null"]},
                                "raw_content_id": {"type": ["integer", "null"]},
                                "metric": {"type": ["string", "null"]},
                            },
                        },
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
        "output_tool": "emit_ceo_bull",
        "max_input_tokens": 18_000,
        "max_output_tokens": 3_000,
        "temperature": 0.1,
        "cost_class": "expensive",
        "stream_response": True,
        "system_prompt": CEO_BULL_PERSONA_V1,
    }
}
