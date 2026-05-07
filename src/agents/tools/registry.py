"""TOOL_REGISTRY — all data-fetching tools available to agent personas.

TOOLS_BACKEND environment variable controls execution mode:
  stub (default) — every executor returns empty results; safe for skeleton runs
                   but produces vacuous LLM outputs. A WARNING is logged at import.
  sql            — real SQL-backed executors (not yet implemented; raises NotImplementedError)

Set TOOLS_BACKEND=sql once src/agents/tools/{news,explorer,fno,equity,orchestration}.py
are implemented.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

_TOOLS_BACKEND = os.environ.get("TOOLS_BACKEND", "stub").lower()
if _TOOLS_BACKEND == "stub":
    log.warning(
        "TOOLS_BACKEND=stub — all 16 tool executors return empty results. "
        "LLM agents will reason over no data. "
        "Set TOOLS_BACKEND=sql to activate the SQL-backed executors."
    )


@dataclass
class ToolDefinition:
    """One tool: LLM-facing schema + async Python executor."""

    name: str
    json_schema: dict           # {"name": ..., "description": ..., "input_schema": ...}
    executor: Callable          # async (params: dict, ctx: ToolContext) -> dict
    timeout_seconds: int = 10
    cost_class: str = "cheap"  # "cheap" | "medium" | "expensive"


TOOL_REGISTRY: dict[str, ToolDefinition] = {}


def register_tool(td: ToolDefinition) -> None:
    if td.name in TOOL_REGISTRY:
        raise ValueError(f"Tool already registered: {td.name!r}")
    TOOL_REGISTRY[td.name] = td


# ---------------------------------------------------------------------------
# Tool schemas (LLM-facing JSON schemas)
# ---------------------------------------------------------------------------

SEARCH_RAW_CONTENT_SCHEMA = {
    "name": "search_raw_content",
    "description": "Search raw news/filing content for one instrument in a time window.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": "integer"},
            "since": {"type": "string", "description": "ISO timestamp"},
            "until": {"type": "string", "description": "ISO timestamp (optional)"},
            "limit": {"type": "integer", "default": 25},
            "min_credibility": {"type": "number", "default": 0.0},
            "include_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by content type: news, filing, transcript, etc.",
            },
        },
        "required": ["instrument_id", "since"],
    },
}

GET_FILINGS_SCHEMA = {
    "name": "get_filings",
    "description": "Retrieve SEBI/BSE/NSE regulatory filings for one instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": "integer"},
            "since": {"type": "string"},
            "filing_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by type: results, corporate_action, announcement, etc.",
            },
        },
        "required": ["instrument_id", "since"],
    },
}

SEARCH_TRANSCRIPT_CHUNKS_SCHEMA = {
    "name": "search_transcript_chunks",
    "description": "Search analyst podcast/video transcript chunks by symbol.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "since": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["symbol", "since"],
    },
}

GET_ANALYST_TRACK_RECORD_SCHEMA = {
    "name": "get_analyst_track_record",
    "description": "Retrieve an analyst's historical credibility score and prediction accuracy.",
    "input_schema": {
        "type": "object",
        "properties": {
            "analyst_id": {"type": "integer"},
        },
        "required": ["analyst_id"],
    },
}

GET_PRICE_AGGREGATES_SCHEMA = {
    "name": "get_price_aggregates",
    "description": "Fetch OHLCV aggregates for one instrument over a window.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": "integer"},
            "window": {
                "type": "string",
                "enum": ["1d_daily_60", "20d_hourly", "5d_intraday_15m"],
                "description": "Pre-defined window + granularity combinations",
            },
        },
        "required": ["instrument_id", "window"],
    },
}

GET_PAST_PREDICTIONS_SCHEMA = {
    "name": "get_past_predictions",
    "description": "Retrieve resolved past agent_predictions for an instrument or sector.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": ["integer", "null"]},
            "sector": {"type": ["string", "null"]},
            "lookback_days": {"type": "integer", "default": 90},
            "only_resolved": {"type": "boolean", "default": True},
        },
    },
}

GET_SENTIMENT_HISTORY_SCHEMA = {
    "name": "get_sentiment_history",
    "description": "Fetch daily sentiment score time-series for one instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": "integer"},
            "since": {"type": "string"},
            "granularity": {"type": "string", "enum": ["daily", "weekly"], "default": "daily"},
        },
        "required": ["instrument_id", "since"],
    },
}

GET_OPTIONS_CHAIN_SCHEMA = {
    "name": "get_options_chain",
    "description": "Fetch the current options chain snapshot for an F&O underlying.",
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying_id": {"type": "integer"},
            "expiry_date": {"type": "string", "description": "ISO date"},
            "snapshot_at": {"type": ["string", "null"], "description": "ISO timestamp (optional)"},
        },
        "required": ["underlying_id", "expiry_date"],
    },
}

GET_IV_CONTEXT_SCHEMA = {
    "name": "get_iv_context",
    "description": "Fetch IV history and HV for an underlying to assess IV richness.",
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying_id": {"type": "integer"},
            "lookback_days": {"type": "integer", "default": 30},
        },
        "required": ["underlying_id"],
    },
}

ENUMERATE_ELIGIBLE_STRATEGIES_SCHEMA = {
    "name": "enumerate_eligible_strategies",
    "description": "List F&O strategies eligible given direction, IV regime, and VIX regime.",
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
            "iv_regime": {"type": "string", "enum": ["cheap", "fair", "rich"]},
            "expiry_days": {"type": "integer"},
            "vix_regime": {"type": "string", "enum": ["low", "neutral", "high"]},
        },
        "required": ["direction", "iv_regime", "expiry_days", "vix_regime"],
    },
}

GET_STRATEGY_PAYOFF_SCHEMA = {
    "name": "get_strategy_payoff",
    "description": "Compute payoff table for a specific options strategy.",
    "input_schema": {
        "type": "object",
        "properties": {
            "strategy_name": {"type": "string"},
            "legs": {"type": "array", "items": {"type": "object"}},
            "expiry_date": {"type": "string"},
            "spot": {"type": "number"},
            "iv_input": {"type": "number"},
        },
        "required": ["strategy_name", "legs", "expiry_date", "spot", "iv_input"],
    },
}

CHECK_BAN_LIST_SCHEMA = {
    "name": "check_ban_list",
    "description": "Check whether an F&O instrument is on the SEBI ban list today.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": "integer"},
        },
        "required": ["instrument_id"],
    },
}

SCORE_TECHNICALS_SCHEMA = {
    "name": "score_technicals",
    "description": "Score the technical setup for an equity instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": "integer"},
            "lookback_days": {"type": "integer", "default": 60},
        },
        "required": ["instrument_id"],
    },
}

SCORE_FUNDAMENTALS_SCHEMA = {
    "name": "score_fundamentals",
    "description": "Score the fundamental valuation for an equity instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument_id": {"type": "integer"},
        },
        "required": ["instrument_id"],
    },
}

POSITION_SIZING_SCHEMA = {
    "name": "position_sizing",
    "description": "Compute recommended position size given conviction and risk params.",
    "input_schema": {
        "type": "object",
        "properties": {
            "account_value_inr": {"type": "number"},
            "target_pct": {"type": "number"},
            "stop_pct": {"type": "number"},
            "conviction": {"type": "number"},
        },
        "required": ["account_value_inr", "target_pct", "stop_pct", "conviction"],
    },
}

GET_FULL_RATIONALE_SCHEMA = {
    "name": "get_full_rationale",
    "description": "Retrieve the full rationale for a prediction or candidate by ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "prediction_or_candidate_id": {"type": "string"},
        },
        "required": ["prediction_or_candidate_id"],
    },
}


# ---------------------------------------------------------------------------
# Executor selection: stub vs SQL
# ---------------------------------------------------------------------------

async def _stub_executor(params: dict, ctx: Any) -> dict:
    """Placeholder executor — returns an empty result. Override per tool."""
    return {"result": [], "note": "stub executor — no DB query implemented yet"}


def _load_sql_executors() -> dict[str, Any]:
    """Import SQL executors lazily.  Only called when TOOLS_BACKEND=sql."""
    from src.agents.tools.news import (
        execute_search_raw_content,
        execute_get_filings,
        execute_search_transcript_chunks,
        execute_get_analyst_track_record,
    )
    from src.agents.tools.explorer import (
        execute_get_price_aggregates,
        execute_get_past_predictions,
        execute_get_sentiment_history,
    )
    from src.agents.tools.fno import (
        execute_get_options_chain,
        execute_get_iv_context,
        execute_enumerate_eligible_strategies,
        execute_get_strategy_payoff,
        execute_check_ban_list,
    )
    from src.agents.tools.equity import (
        execute_score_technicals,
        execute_score_fundamentals,
        execute_position_sizing,
    )
    from src.agents.tools.orchestration import execute_get_full_rationale

    return {
        "search_raw_content":          execute_search_raw_content,
        "get_filings":                 execute_get_filings,
        "search_transcript_chunks":    execute_search_transcript_chunks,
        "get_analyst_track_record":    execute_get_analyst_track_record,
        "get_price_aggregates":        execute_get_price_aggregates,
        "get_past_predictions":        execute_get_past_predictions,
        "get_sentiment_history":       execute_get_sentiment_history,
        "get_options_chain":           execute_get_options_chain,
        "get_iv_context":              execute_get_iv_context,
        "enumerate_eligible_strategies": execute_enumerate_eligible_strategies,
        "get_strategy_payoff":         execute_get_strategy_payoff,
        "check_ban_list":              execute_check_ban_list,
        "score_technicals":            execute_score_technicals,
        "score_fundamentals":          execute_score_fundamentals,
        "position_sizing":             execute_position_sizing,
        "get_full_rationale":          execute_get_full_rationale,
    }


_SQL_EXECUTORS: dict[str, Any] = {}
if _TOOLS_BACKEND == "sql":
    try:
        _SQL_EXECUTORS = _load_sql_executors()
        log.info("TOOLS_BACKEND=sql — loaded %d SQL executors", len(_SQL_EXECUTORS))
    except ImportError as e:
        log.error("TOOLS_BACKEND=sql but SQL executor import failed: %s — falling back to stubs", e)


def _executor_for(name: str) -> Any:
    """Return the SQL executor if available, else the stub."""
    return _SQL_EXECUTORS.get(name, _stub_executor)


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------

_TOOLS_TO_REGISTER = [
    ("search_raw_content", SEARCH_RAW_CONTENT_SCHEMA),
    ("get_filings", GET_FILINGS_SCHEMA),
    ("search_transcript_chunks", SEARCH_TRANSCRIPT_CHUNKS_SCHEMA),
    ("get_analyst_track_record", GET_ANALYST_TRACK_RECORD_SCHEMA),
    ("get_price_aggregates", GET_PRICE_AGGREGATES_SCHEMA),
    ("get_past_predictions", GET_PAST_PREDICTIONS_SCHEMA),
    ("get_sentiment_history", GET_SENTIMENT_HISTORY_SCHEMA),
    ("get_options_chain", GET_OPTIONS_CHAIN_SCHEMA),
    ("get_iv_context", GET_IV_CONTEXT_SCHEMA),
    ("enumerate_eligible_strategies", ENUMERATE_ELIGIBLE_STRATEGIES_SCHEMA),
    ("get_strategy_payoff", GET_STRATEGY_PAYOFF_SCHEMA),
    ("check_ban_list", CHECK_BAN_LIST_SCHEMA),
    ("score_technicals", SCORE_TECHNICALS_SCHEMA),
    ("score_fundamentals", SCORE_FUNDAMENTALS_SCHEMA),
    ("position_sizing", POSITION_SIZING_SCHEMA),
    ("get_full_rationale", GET_FULL_RATIONALE_SCHEMA),
]

for _name, _schema in _TOOLS_TO_REGISTER:
    register_tool(ToolDefinition(
        name=_name,
        json_schema=_schema,
        executor=_executor_for(_name),
        timeout_seconds=10,
        cost_class="cheap",
    ))
