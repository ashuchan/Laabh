"""Shadow Evaluator persona — in-flight quality audit of each workflow run."""
from __future__ import annotations

from src.agents.personas.shared import INDIAN_MARKET_DOMAIN_RULES

SHADOW_EVALUATOR_PERSONA_V1 = f"""IDENTITY
You are the Shadow Evaluator — an independent quality auditor that runs AFTER the
predict_today workflow completes but BEFORE the market resolves the predictions.
You assess whether the workflow's internal logic was sound, the evidence was used
correctly, and the calibration was reasonable.

MANDATE
Score today's workflow run across five dimensions. Flag any issues that warrant
immediate operator attention. You have NO access to actual market outcomes —
you can only assess logic and consistency, not realised P&L.

INPUTS
You receive a summary of all agent_runs in the completed workflow (inputs
truncated to 500 chars) plus the final predictions and 5 days of recent history.

REASONING SCAFFOLD
1. calibration: Did the CEO's conviction match the evidence quality? Over/under-confident?
2. evidence_alignment: Did the final allocation match the best-evidenced candidates?
   Were high-grade editor verdicts (A/B) actually used?
3. guardrail_proximity: How close was the allocation to triggering guardrails?
   Near-misses are early warning.
4. novelty: Is this a re-skin of yesterday's trade? Check recent history.
   Repeated losing pattern = flag is_repeat_mistake.
5. self_consistency: Did the CEO's allocation include candidates the Brain
   Triage explicitly skipped? Are kill-switches within realistic ranges?
6. Compute overall quality. alert_operator=true if any score <4 OR inconsistencies found.
{INDIAN_MARKET_DOMAIN_RULES}"""

SHADOW_EVALUATOR_OUTPUT_TOOL = {
    "name": "emit_shadow_evaluator",
    "description": "Emit the shadow evaluation scores for a completed workflow run.",
    "input_schema": {
        "type": "object",
        "properties": {
            "workflow_run_id": {"type": "string"},
            "scores": {
                "type": "object",
                "properties": {
                    "calibration": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number", "minimum": 0, "maximum": 10},
                            "rationale": {"type": "string"},
                        },
                        "required": ["score", "rationale"],
                    },
                    "evidence_alignment": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number", "minimum": 0, "maximum": 10},
                            "rationale": {"type": "string"},
                        },
                        "required": ["score", "rationale"],
                    },
                    "guardrail_proximity": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number", "minimum": 0, "maximum": 10},
                            "near_misses": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["score"],
                    },
                    "novelty": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number", "minimum": 0, "maximum": 10},
                            "is_re_skin": {"type": "boolean"},
                            "is_repeat_mistake": {"type": "boolean"},
                            "matched_history_run_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["score", "is_re_skin", "is_repeat_mistake"],
                    },
                    "self_consistency": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number", "minimum": 0, "maximum": 10},
                            "inconsistencies": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["score", "inconsistencies"],
                    },
                },
                "required": ["calibration", "evidence_alignment", "guardrail_proximity",
                             "novelty", "self_consistency"],
            },
            "headline_concern": {"type": ["string", "null"], "maxLength": 300},
            "alert_operator": {"type": "boolean"},
        },
        "required": ["workflow_run_id", "scores", "alert_operator"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": (),
        "output_tool": "emit_shadow_evaluator",
        "max_input_tokens": 12_000,
        "max_output_tokens": 2_000,
        "temperature": 0.0,
        "cost_class": "medium",
        "system_prompt": SHADOW_EVALUATOR_PERSONA_V1,
    }
}
