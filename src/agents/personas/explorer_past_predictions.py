"""Explorer Past Predictions sub-agent — learning from prior predictions."""
from __future__ import annotations

EXPLORER_PAST_PREDICTIONS_PERSONA_V1 = """IDENTITY
You are the track-record analyst in the Historical Explorer pod. You review the
desk's prior predictions for this instrument to extract lessons, calibrate
conviction, and identify patterns that should not be repeated.

MANDATE
Surface actionable lessons from past predictions on this instrument or its sector.
Focus on what made wins work and what made losses fail — not just outcomes.

INPUTS
You receive the candidate dict. Call get_past_predictions to fetch resolved
prediction history.

REASONING SCAFFOLD
1. Fetch past resolved predictions for this instrument (lookback 90 days).
2. Also fetch predictions for the same sector (lookback 30 days).
3. Compute win rate, mean P&L, conviction calibration.
4. Identify the single biggest win and its setup. What worked?
5. Identify the single biggest loss and its mistake. What went wrong?
6. Flag any patterns that failed 2+ times in a row (do_not_repeat).
"""

EXPLORER_PAST_PREDICTIONS_OUTPUT_TOOL = {
    "name": "emit_explorer_past_predictions",
    "description": "Emit past prediction analysis for one instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "stats": {
                "type": "object",
                "properties": {
                    "n_predictions": {"type": "integer"},
                    "win_rate": {"type": "number"},
                    "mean_pnl_pct": {"type": "number"},
                    "lookback_days": {"type": "integer"},
                },
                "required": ["n_predictions", "win_rate"],
            },
            "conviction_calibration": {
                "type": "string",
                "enum": ["well_calibrated", "overconfident", "underconfident", "insufficient_data"],
            },
            "biggest_win": {"type": ["object", "null"]},
            "biggest_loss": {"type": ["object", "null"]},
            "tradable_patterns": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string"},
            },
            "do_not_repeat": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string"},
            },
            "tldr": {"type": "string", "maxLength": 120},
        },
        "required": ["symbol", "stats", "conviction_calibration", "do_not_repeat"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": ("get_past_predictions",),
        "output_tool": "emit_explorer_past_predictions",
        "max_input_tokens": 8_000,
        "max_output_tokens": 1_500,
        "temperature": 0.0,
        "cost_class": "medium",
        "system_prompt": EXPLORER_PAST_PREDICTIONS_PERSONA_V1,
    }
}
