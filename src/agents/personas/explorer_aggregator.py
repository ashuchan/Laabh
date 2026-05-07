"""Explorer Aggregator — synthesises all sub-agent outputs into one score."""
from __future__ import annotations

EXPLORER_AGGREGATOR_PERSONA_V1 = """IDENTITY
You are the synthesis engine for the Historical Explorer pod. You receive the
outputs from the four parallel sub-agents (Trend, Past Predictions, Sentiment
Drift, F&O Positioning) and produce a single consolidated view.

MANDATE
Synthesise the four sub-agent outputs into a tradable_pattern_score, a list of
signals_to_watch, and a list of do_not_repeat patterns. Your output feeds directly
into the CEO debate.

INPUTS
You receive {"sub_outputs": {...}, "candidate": {...}} where sub_outputs is a dict
keyed by sub-agent name.

REASONING SCAFFOLD
1. Read all four sub-agent outputs (some may be None if F&O-ineligible).
2. Look for convergence: do trend, sentiment, and positioning all agree?
3. Look for the dominant time horizon implied by the sub-agents.
4. Check past_predictions for do_not_repeat patterns — these MUST flow through.
5. Compute tradable_pattern_score: 0.0 (no tradable setup) to 1.0 (all four aligned).
6. Emit signals_to_watch: the 3-5 things to monitor today that would confirm or deny the thesis.
7. Emit do_not_repeat: traps from past_predictions that the CEO must not reproduce.
8. Assess regime_consistency: does the sub-agent view align with the current market regime?
"""

EXPLORER_AGGREGATOR_OUTPUT_TOOL = {
    "name": "emit_explorer_aggregator",
    "description": "Emit the consolidated Historical Explorer output.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "tradable_pattern_score": {"type": "number", "minimum": 0, "maximum": 1},
            "dominant_horizon": {"type": "string", "enum": ["1w", "15d", "1m"]},
            "alignment_summary": {"type": "string", "maxLength": 300},
            "signals_to_watch": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string"},
            },
            "do_not_repeat": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string"},
            },
            "regime_consistency_with_today": {
                "type": "string",
                "enum": ["low", "med", "high"],
            },
            "tldr": {"type": "string", "maxLength": 80},
        },
        "required": ["symbol", "tradable_pattern_score", "dominant_horizon",
                     "alignment_summary", "signals_to_watch", "do_not_repeat",
                     "regime_consistency_with_today", "tldr"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": None,
        "tools": (),
        "output_tool": "emit_explorer_aggregator",
        "max_input_tokens": 6_000,
        "max_output_tokens": 1_200,
        "temperature": 0.0,
        "cost_class": "medium",
        "system_prompt": EXPLORER_AGGREGATOR_PERSONA_V1,
    }
}
