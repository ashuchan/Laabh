"""CEO Judge persona — final allocation decision from the bull/bear debate."""
from __future__ import annotations

CEO_JUDGE_PERSONA_V1 = """IDENTITY
You are the Chief Investment Strategist. You have read the Bull and Bear portfolio
managers' arguments. Your job is to adjudicate the debate and produce the final
allocation decision for today. You are biased toward being right, not toward
being aggressive or cautious.

MANDATE
Read the Bull and Bear briefs. Adjudicate. Produce the final allocation and kill-
switches. The kill-switches must come directly from the Bear's
what_would_change_my_mind list — this is not ad hoc.

INPUTS
You receive the Bull brief, the Bear brief, and the full portfolio context
(same cached data packet). Call get_full_rationale for any specific claim.

REASONING SCAFFOLD
1. For each material disagreement: identify which side had stronger evidence.
2. Weight evidence quality over argument style.
3. Construct the allocation: resolve disagreements case by case.
4. Verify: does the allocation sum to ≤100% capital? Is any single leg >30%?
5. Lift kill-switches verbatim from the Bear's what_would_change_my_mind.
6. Self-audit: grade both arguments (A-D), state your own confidence (0-1),
   and articulate the asymmetric regret (which direction is worse to be wrong in?).
7. Write the CEO note: 5 sentences for a human reader. No jargon.
"""

CEO_JUDGE_OUTPUT_TOOL = {
    "name": "emit_ceo_judge",
    "description": "Emit the final CEO Judge allocation verdict.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_summary": {"type": "string", "maxLength": 600},
            "disagreement_loci": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "bull_view": {"type": "string"},
                        "bear_view": {"type": "string"},
                        "judge_lean": {"type": "string", "enum": ["bull", "bear", "split"]},
                        "lean_strength": {"type": "string", "enum": ["weak", "medium", "strong"]},
                        "decisive_evidence": {"type": "string"},
                    },
                    "required": ["topic", "judge_lean", "lean_strength"],
                },
            },
            "allocation": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "asset_class": {"type": "string", "enum": ["fno", "equity", "cash"]},
                        "underlying_or_symbol": {"type": "string"},
                        "capital_pct": {"type": "number", "minimum": 0, "maximum": 100},
                        "decision": {"type": "string"},
                        "horizon": {"type": ["string", "null"]},
                        "conviction": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["asset_class", "capital_pct", "decision"],
                },
            },
            "expected_book_pnl_pct": {"type": "number"},
            "stretch_pnl_pct": {"type": "number"},
            "max_drawdown_tolerated_pct": {"type": "number"},
            "kill_switches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "trigger": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["exit_all", "scale_down_50", "tighten_stops"],
                        },
                        "monitoring_metric": {"type": "string"},
                    },
                    "required": ["trigger", "action", "monitoring_metric"],
                },
            },
            "ceo_note": {"type": "string", "maxLength": 600},
            "calibration_self_check": {
                "type": "object",
                "properties": {
                    "bullish_argument_grade": {"type": "string", "enum": ["A", "B", "C", "D"]},
                    "bearish_argument_grade": {"type": "string", "enum": ["A", "B", "C", "D"]},
                    "confidence_in_allocation": {"type": "number", "minimum": 0, "maximum": 1},
                    "regret_scenario": {"type": "string", "maxLength": 200},
                },
                "required": ["bullish_argument_grade", "bearish_argument_grade",
                             "confidence_in_allocation", "regret_scenario"],
            },
        },
        "required": ["decision_summary", "allocation", "kill_switches",
                     "ceo_note", "calibration_self_check",
                     "expected_book_pnl_pct", "max_drawdown_tolerated_pct"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-opus-4-7",
        "fallback_model": "claude-sonnet-4-6",
        "tools": ("get_full_rationale",),
        "output_tool": "emit_ceo_judge",
        "max_input_tokens": 22_000,
        "max_output_tokens": 4_000,
        "temperature": 0.0,
        "cost_class": "expensive",
        "stream_response": True,
        "output_validator": "CEOJudgeOutputValidated",
        "system_prompt": CEO_JUDGE_PERSONA_V1,
    }
}
