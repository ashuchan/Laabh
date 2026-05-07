"""F&O Expert persona — produces a full options strategy thesis."""
from __future__ import annotations

from src.agents.personas.shared import INDIAN_MARKET_DOMAIN_RULES

FNO_EXPERT_PERSONA_V1 = f"""IDENTITY
You are the head of derivatives structuring for an Indian paper-trading desk.
For one F&O candidate, you design the best risk-adjusted options strategy given
today's thesis, IV environment, and capital constraints.

MANDATE
For the given F&O candidate, produce ONE specific options strategy with legs,
strikes, expiry, economics, and a binary kill-switch. Refuse if the risk-reward
does not clear 3× transaction costs or if IV is too rich for the direction.

INPUTS
You receive one F&O candidate from the Brain Triage, enriched with News Finder,
Editor, and Explorer outputs. Call your tools to enumerate eligible strategies
and compute payoffs.

REASONING SCAFFOLD
1. Read the candidate's expected_strategy_family from Brain Triage.
2. Check the current IV regime (cheap/fair/rich) from Explorer F&O output.
3. Call enumerate_eligible_strategies to get a short-list of valid structures.
4. For each structure: call get_strategy_payoff to get expected P&L at expiry.
5. Select the structure with best risk-adjusted expected P&L.
6. Verify against transaction costs: gross expected P&L must be >3x costs.
7. Define kill-switch: specific price level or time at which to exit immediately.
8. Refuse if: IV rich + direction unclear, OR PCR extreme + no catalyst to move it.

{INDIAN_MARKET_DOMAIN_RULES}

CALIBRATION
conviction: 0.90+ = extraordinary alignment; 0.75-0.89 = high; 0.60-0.74 = workable.
expected_10pct_probability: probability the trade earns >10% in the given horizon.
For debit spreads: max loss = debit paid; expected win = credit at target.
"""

FNO_EXPERT_OUTPUT_TOOL = {
    "name": "emit_fno_expert",
    "description": "Emit the full F&O strategy thesis for one candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "strategy": {"type": "string"},
            "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
            "conviction": {"type": "number", "minimum": 0, "maximum": 1},
            "expected_10pct_probability": {"type": "number", "minimum": 0, "maximum": 1},
            "legs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["BUY", "SELL"]},
                        "option_type": {"type": "string", "enum": ["CE", "PE"]},
                        "strike": {"type": "number"},
                        "expiry": {"type": "string"},
                        "lots": {"type": "integer"},
                    },
                    "required": ["action", "option_type", "strike", "expiry", "lots"],
                },
            },
            "economics": {
                "type": "object",
                "properties": {
                    "max_loss_pct": {"type": "number"},
                    "target_pnl_pct": {"type": "number"},
                    "breakeven": {"type": "number"},
                    "transaction_cost_inr": {"type": "number"},
                },
                "required": ["max_loss_pct", "target_pnl_pct"],
            },
            "kill_switch": {
                "type": "object",
                "properties": {
                    "trigger_price": {"type": "number"},
                    "trigger_type": {"type": "string", "enum": ["spot_below", "spot_above", "time"]},
                    "action": {"type": "string"},
                },
                "required": ["trigger_type", "action"],
            },
            "thesis": {"type": "string", "maxLength": 500},
            "refused": {"type": "boolean"},
            "refuse_reason": {"type": ["string", "null"]},
        },
        "required": ["symbol", "strategy", "direction", "conviction", "legs", "economics",
                     "kill_switch", "thesis", "refused"],
    },
}

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": ("enumerate_eligible_strategies", "get_strategy_payoff", "check_ban_list"),
        "output_tool": "emit_fno_expert",
        "max_input_tokens": 12_000,
        "max_output_tokens": 2_500,
        "temperature": 0.1,
        "cost_class": "medium",
        "system_prompt": FNO_EXPERT_PERSONA_V1,
    }
}
