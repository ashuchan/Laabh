"""Tests for F&O thesis synthesizer Phase 3 — pure helpers only (no LLM calls)."""
from __future__ import annotations

import pytest

from src.fno.thesis_synthesizer import (
    build_user_prompt,
    classify_oi_structure,
    parse_llm_response,
)


# ---------------------------------------------------------------------------
# parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_llm_response_proceed() -> None:
    raw = '{"decision": "PROCEED", "direction": "bullish", "thesis": "Strong momentum.", "risk_factors": ["gap risk"], "confidence": 0.75}'
    result = parse_llm_response(raw)
    assert result["decision"] == "PROCEED"
    assert result["direction"] == "bullish"
    assert result["confidence"] == 0.75
    assert result["risk_factors"] == ["gap risk"]


def test_parse_llm_response_skip() -> None:
    raw = '{"decision": "skip", "direction": "neutral", "thesis": "No edge.", "risk_factors": [], "confidence": 0.3}'
    result = parse_llm_response(raw)
    assert result["decision"] == "SKIP"


def test_parse_llm_response_invalid_decision_defaults_to_skip() -> None:
    raw = '{"decision": "YOLO", "direction": "bullish", "thesis": ".", "risk_factors": [], "confidence": 0.9}'
    result = parse_llm_response(raw)
    assert result["decision"] == "SKIP"


def test_parse_llm_response_hedge() -> None:
    raw = '{"decision": "HEDGE", "direction": "neutral", "thesis": "Mixed signals.", "risk_factors": ["VIX spike"], "confidence": 0.5}'
    result = parse_llm_response(raw)
    assert result["decision"] == "HEDGE"


def test_parse_llm_response_truncates_long_thesis() -> None:
    long_thesis = "X" * 600
    raw = f'{{"decision": "PROCEED", "direction": "bullish", "thesis": "{long_thesis}", "risk_factors": [], "confidence": 0.8}}'
    result = parse_llm_response(raw)
    assert len(result["thesis"]) <= 500


def test_parse_llm_response_caps_risk_factors_at_3() -> None:
    raw = '{"decision": "PROCEED", "direction": "bullish", "thesis": ".", "risk_factors": ["a", "b", "c", "d", "e"], "confidence": 0.7}'
    result = parse_llm_response(raw)
    assert len(result["risk_factors"]) == 3


# ---------------------------------------------------------------------------
# classify_oi_structure
# ---------------------------------------------------------------------------

def test_classify_oi_structure_put_heavy() -> None:
    assert classify_oi_structure(1.5) == "put_heavy"


def test_classify_oi_structure_call_heavy() -> None:
    assert classify_oi_structure(0.5) == "call_heavy"


def test_classify_oi_structure_balanced() -> None:
    assert classify_oi_structure(1.0) == "balanced"


def test_classify_oi_structure_none_is_unknown() -> None:
    assert classify_oi_structure(None) == "unknown"


def test_classify_oi_structure_boundary_put_heavy() -> None:
    # PCR = 1.3 is exactly at boundary → balanced (strictly > 1.3 required)
    assert classify_oi_structure(1.3) == "balanced"


def test_classify_oi_structure_boundary_call_heavy() -> None:
    # PCR = 0.7 is exactly at boundary → balanced (strictly < 0.7 required)
    assert classify_oi_structure(0.7) == "balanced"


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------

def test_build_user_prompt_contains_symbol() -> None:
    prompt = build_user_prompt(
        symbol="NIFTY",
        sector="Index",
        underlying_price=22000.0,
        iv_rank=45.0,
        iv_regime="neutral",
        oi_structure="balanced",
        days_to_expiry=3,
        news_score=6.5,
        sentiment_score=5.5,
        fii_dii_score=7.0,
        macro_align_score=6.0,
        convergence_score=6.5,
        composite_score=6.4,
        bullish_count=3,
        bearish_count=1,
        lookback_hours=18,
        fii_net_cr=1200.0,
        dii_net_cr=800.0,
        macro_drivers=["SPX_FUTURES"],
        headlines=["NIFTY hits all-time high"],
    )
    assert "NIFTY" in prompt
    assert "22,000.00" in prompt
    assert "NIFTY hits all-time high" in prompt


def test_build_user_prompt_empty_headlines() -> None:
    prompt = build_user_prompt(
        symbol="RELIANCE",
        sector="Energy",
        underlying_price=3000.0,
        iv_rank=60.0,
        iv_regime="high",
        oi_structure="call_heavy",
        days_to_expiry=5,
        news_score=5.0,
        sentiment_score=5.0,
        fii_dii_score=5.0,
        macro_align_score=5.0,
        convergence_score=5.0,
        composite_score=5.0,
        bullish_count=0,
        bearish_count=0,
        lookback_hours=18,
        fii_net_cr=0.0,
        dii_net_cr=0.0,
        macro_drivers=["BRENT", "WTI"],
        headlines=[],
    )
    assert "no recent headlines" in prompt


def test_build_user_prompt_threads_market_movers_block() -> None:
    movers_block = (
        "YESTERDAY'S F&O LEADERS (2026-05-07):\n"
        "   1. PAYTM        +7.82%  ₹1,110.60 → ₹1,197.40\n"
        "THIS INSTRUMENT YESTERDAY: PAYTM was rank #1 gainer (+7.82%).\n"
    )
    prompt = build_user_prompt(
        symbol="PAYTM",
        sector="Fintech",
        underlying_price=1197.40,
        iv_rank=55.0,
        iv_regime="neutral",
        oi_structure="balanced",
        days_to_expiry=4,
        news_score=6.0,
        sentiment_score=6.0,
        fii_dii_score=5.0,
        macro_align_score=5.0,
        convergence_score=5.5,
        composite_score=5.6,
        bullish_count=2,
        bearish_count=0,
        lookback_hours=18,
        fii_net_cr=500.0,
        dii_net_cr=300.0,
        macro_drivers=[],
        headlines=["PAYTM Q4 beat"],
        market_movers_context=movers_block,
    )
    assert "F&O LEADERS" in prompt
    assert "rank #1 gainer" in prompt
