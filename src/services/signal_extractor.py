"""
Signal extractor service — enriches extracted signals with multi-agent debate verdict.
Wraps TradingAgents debate pipeline into the existing signal flow.
"""
from __future__ import annotations

from datetime import date

from loguru import logger

from src.integrations.tradingagents.debate import debate_signal


async def enrich_with_debate(ticker: str, existing_signal: dict) -> dict:
    """
    After RSS/news extraction, run the multi-agent debate.
    Only called for signals that cleared the convergence_score >= 2 threshold.

    Args:
        ticker: NSE symbol e.g. "RELIANCE"
        existing_signal: signal dict from the LLM extractor pipeline

    Returns:
        Updated signal dict with debate fields added.
    """
    try:
        verdict = debate_signal(ticker, date.today().isoformat())
    except Exception as exc:
        logger.warning(f"debate_signal failed for {ticker}: {exc}")
        return existing_signal

    existing_signal["debate_decision"] = verdict["decision"]
    existing_signal["debate_confidence"] = verdict["confidence"]
    existing_signal["bull_thesis"] = verdict["bull_thesis"]
    existing_signal["bear_thesis"] = verdict["bear_thesis"]
    existing_signal["risk_assessment"] = verdict["risk_assessment"]

    if _aligns(existing_signal.get("direction", ""), verdict["decision"]):
        existing_signal["convergence_score"] = (
            existing_signal.get("convergence_score", 0) + 1.5
        )
        logger.debug(
            f"debate aligned for {ticker}: {verdict['decision']}, "
            f"convergence boosted to {existing_signal['convergence_score']}"
        )

    return existing_signal


def _aligns(direction: str, decision: str) -> bool:
    """Return True when debate decision aligns with signal direction."""
    bullish = {"Buy", "Overweight"}
    bearish = {"Sell", "Underweight"}
    return (direction == "BUY" and decision in bullish) or (
        direction == "SELL" and decision in bearish
    )
