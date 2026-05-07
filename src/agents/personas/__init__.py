"""PERSONA_MANIFEST and OUTPUT_TOOL_SCHEMAS — the registry of all agent personas."""
from __future__ import annotations

from src.agents.personas.brain_triage import (
    PERSONA_DEF as _BRAIN_TRIAGE,
    BRAIN_TRIAGE_OUTPUT_TOOL,
)
from src.agents.personas.news_finder import (
    PERSONA_DEF as _NEWS_FINDER,
    NEWS_FINDER_OUTPUT_TOOL,
)
from src.agents.personas.news_editor import (
    PERSONA_DEF as _NEWS_EDITOR,
    NEWS_EDITOR_OUTPUT_TOOL,
)
from src.agents.personas.explorer_trend import (
    PERSONA_DEF as _EXPLORER_TREND,
    EXPLORER_TREND_OUTPUT_TOOL,
)
from src.agents.personas.explorer_past_predictions import (
    PERSONA_DEF as _EXPLORER_PAST_PREDICTIONS,
    EXPLORER_PAST_PREDICTIONS_OUTPUT_TOOL,
)
from src.agents.personas.explorer_sentiment_drift import (
    PERSONA_DEF as _EXPLORER_SENTIMENT_DRIFT,
    EXPLORER_SENTIMENT_DRIFT_OUTPUT_TOOL,
)
from src.agents.personas.explorer_fno_positioning import (
    PERSONA_DEF as _EXPLORER_FNO_POSITIONING,
    EXPLORER_FNO_POSITIONING_OUTPUT_TOOL,
)
from src.agents.personas.explorer_aggregator import (
    PERSONA_DEF as _EXPLORER_AGGREGATOR,
    EXPLORER_AGGREGATOR_OUTPUT_TOOL,
)
from src.agents.personas.fno_expert import (
    PERSONA_DEF as _FNO_EXPERT,
    FNO_EXPERT_OUTPUT_TOOL,
)
from src.agents.personas.equity_expert import (
    PERSONA_DEF as _EQUITY_EXPERT,
    EQUITY_EXPERT_OUTPUT_TOOL,
)
from src.agents.personas.ceo_bull import (
    PERSONA_DEF as _CEO_BULL,
    CEO_BULL_OUTPUT_TOOL,
)
from src.agents.personas.ceo_bear import (
    PERSONA_DEF as _CEO_BEAR,
    CEO_BEAR_OUTPUT_TOOL,
)
from src.agents.personas.ceo_judge import (
    PERSONA_DEF as _CEO_JUDGE,
    CEO_JUDGE_OUTPUT_TOOL,
)
from src.agents.personas.shadow_evaluator import (
    PERSONA_DEF as _SHADOW_EVALUATOR,
    SHADOW_EVALUATOR_OUTPUT_TOOL,
)

# PERSONA_MANIFEST: {agent_name: {version: persona_def_dict}}
PERSONA_MANIFEST: dict[str, dict[str, dict]] = {
    "brain_triage": _BRAIN_TRIAGE,
    "news_finder": _NEWS_FINDER,
    "news_editor": _NEWS_EDITOR,
    "explorer_trend": _EXPLORER_TREND,
    "explorer_past_predictions": _EXPLORER_PAST_PREDICTIONS,
    "explorer_sentiment_drift": _EXPLORER_SENTIMENT_DRIFT,
    "explorer_fno_positioning": _EXPLORER_FNO_POSITIONING,
    "explorer_aggregator": _EXPLORER_AGGREGATOR,
    "fno_expert": _FNO_EXPERT,
    "equity_expert": _EQUITY_EXPERT,
    "ceo_bull": _CEO_BULL,
    "ceo_bear": _CEO_BEAR,
    "ceo_judge": _CEO_JUDGE,
    "shadow_evaluator": _SHADOW_EVALUATOR,
}

# OUTPUT_TOOL_SCHEMAS: {tool_name: json_schema_dict}
OUTPUT_TOOL_SCHEMAS: dict[str, dict] = {
    "emit_brain_triage": BRAIN_TRIAGE_OUTPUT_TOOL,
    "emit_news_finder": NEWS_FINDER_OUTPUT_TOOL,
    "emit_news_editor": NEWS_EDITOR_OUTPUT_TOOL,
    "emit_explorer_trend": EXPLORER_TREND_OUTPUT_TOOL,
    "emit_explorer_past_predictions": EXPLORER_PAST_PREDICTIONS_OUTPUT_TOOL,
    "emit_explorer_sentiment_drift": EXPLORER_SENTIMENT_DRIFT_OUTPUT_TOOL,
    "emit_explorer_fno_positioning": EXPLORER_FNO_POSITIONING_OUTPUT_TOOL,
    "emit_explorer_aggregator": EXPLORER_AGGREGATOR_OUTPUT_TOOL,
    "emit_fno_expert": FNO_EXPERT_OUTPUT_TOOL,
    "emit_equity_expert": EQUITY_EXPERT_OUTPUT_TOOL,
    "emit_ceo_bull": CEO_BULL_OUTPUT_TOOL,
    "emit_ceo_bear": CEO_BEAR_OUTPUT_TOOL,
    "emit_ceo_judge": CEO_JUDGE_OUTPUT_TOOL,
    "emit_shadow_evaluator": SHADOW_EVALUATOR_OUTPUT_TOOL,
}

__all__ = ["PERSONA_MANIFEST", "OUTPUT_TOOL_SCHEMAS"]
