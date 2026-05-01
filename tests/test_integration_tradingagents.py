"""Integration tests for TradingAgents debate adapter."""
from __future__ import annotations

import pytest


@pytest.mark.integration
def test_debate_returns_structure():
    from src.integrations.tradingagents.debate import debate_signal

    result = debate_signal("RELIANCE", "2026-05-01")
    assert "decision" in result
    assert result["decision"] in ("Buy", "Overweight", "Hold", "Underweight", "Sell")
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["bull_thesis"], str)
    assert isinstance(result["bear_thesis"], str)
    assert isinstance(result["risk_assessment"], str)
    assert "agent_verdicts" in result


@pytest.mark.integration
def test_debate_with_nifty50_stock():
    from src.integrations.tradingagents.debate import debate_signal

    result = debate_signal("INFY", "2026-05-01")
    assert result["ticker"] == "INFY"
    assert result["date"] == "2026-05-01"


async def test_debate_health():
    from src.integrations.tradingagents.debate import health

    result = await health()
    assert result["status"] in ("ok", "down")
    assert "backend" in result


def test_parse_confidence_mapping():
    from src.integrations.tradingagents.debate import _parse_confidence

    assert _parse_confidence({"final_trade_decision": "Buy"}) == 0.85
    assert _parse_confidence({"final_trade_decision": "Sell"}) == 0.15
    assert _parse_confidence({"final_trade_decision": "Hold"}) == 0.50
    assert _parse_confidence({}) == 0.50
