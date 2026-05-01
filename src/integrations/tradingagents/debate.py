"""
Multi-agent signal debate adapter.
TradingAgents is Apache 2.0 — freely importable.

Usage: call debate_signal(ticker) to get a structured verdict dict
       from the 5-agent debate pipeline.
"""
from __future__ import annotations

try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph  # type: ignore[import-not-found]
    from tradingagents.default_config import DEFAULT_CONFIG  # type: ignore[import-not-found]
    _TRADINGAGENTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    TradingAgentsGraph = None  # type: ignore[assignment]
    DEFAULT_CONFIG = {}  # type: ignore[assignment]
    _TRADINGAGENTS_AVAILABLE = False


def _build_config() -> dict:
    cfg = DEFAULT_CONFIG.copy() if isinstance(DEFAULT_CONFIG, dict) else {}
    cfg["llm_provider"] = "anthropic"
    cfg["deep_think_llm"] = "claude-sonnet-4-6"
    cfg["quick_think_llm"] = "claude-haiku-4-5-20251001"
    cfg["max_debate_rounds"] = 1
    cfg["online_tools"] = False

    # India-specific analyst prompts (imported lazily to avoid circular deps)
    try:
        from src.integrations.vibetrade.prompts import (
            FUNDAMENTALS_ANALYST_INDIA,
            SENTIMENT_ANALYST_INDIA,
            TECHNICAL_ANALYST_INDIA,
        )
        cfg["analyst_system_prompts"] = {
            "fundamentals": FUNDAMENTALS_ANALYST_INDIA,
            "sentiment": SENTIMENT_ANALYST_INDIA,
            "technical": TECHNICAL_ANALYST_INDIA,
        }
    except ImportError:
        pass

    return cfg


_graph: "TradingAgentsGraph | None" = None


def _get_graph() -> "TradingAgentsGraph":
    global _graph
    if _graph is None:
        if not _TRADINGAGENTS_AVAILABLE:
            raise RuntimeError(
                "tradingagents package is not installed — run: pip install tradingagents"
            )
        _graph = TradingAgentsGraph(debug=False, config=_build_config())
    return _graph


def debate_signal(ticker_nse: str, analysis_date: str) -> dict:
    """
    Run the 5-agent debate for an NSE ticker.

    Args:
        ticker_nse: NSE symbol e.g. "RELIANCE" (not RELIANCE.NS)
        analysis_date: ISO date string e.g. "2026-05-01"

    Returns:
        {
          "ticker": str,
          "date": str,
          "decision": "Buy"|"Overweight"|"Hold"|"Underweight"|"Sell",
          "confidence": float,   # 0.0–1.0
          "bull_thesis": str,
          "bear_thesis": str,
          "risk_assessment": str,
          "agent_verdicts": dict  # per-agent raw output
        }
    """
    graph = _get_graph()
    state, decision = graph.propagate(ticker_nse, analysis_date)

    return {
        "ticker": ticker_nse,
        "date": analysis_date,
        "decision": state.get("final_trade_decision", "Hold"),
        "confidence": _parse_confidence(state),
        "bull_thesis": state.get("bull_researcher_report", ""),
        "bear_thesis": state.get("bear_researcher_report", ""),
        "risk_assessment": state.get("risk_debate_state", ""),
        "agent_verdicts": {
            "fundamentals": state.get("fundamentals_report", ""),
            "sentiment": state.get("sentiment_report", ""),
            "news": state.get("news_report", ""),
            "technicals": state.get("market_report", ""),
        },
    }


def _parse_confidence(state: dict) -> float:
    """Extract numeric confidence from the trader decision string."""
    decision = state.get("final_trade_decision", "Hold")
    mapping = {
        "Buy": 0.85,
        "Overweight": 0.70,
        "Hold": 0.50,
        "Underweight": 0.35,
        "Sell": 0.15,
    }
    return mapping.get(decision, 0.50)


async def health() -> dict:
    """Return integration health status."""
    return {
        "status": "ok" if _TRADINGAGENTS_AVAILABLE else "down",
        "backend": "tradingagents",
        "version": "0.2.4+",
        "available": _TRADINGAGENTS_AVAILABLE,
    }
