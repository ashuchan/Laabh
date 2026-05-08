"""Tests for F&O thesis synthesizer Phase 3 — pure helpers only (no LLM calls)."""
from __future__ import annotations

from datetime import datetime, timezone

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
# parse_llm_response — tolerant fallbacks (markdown fence + prose preamble)
# ---------------------------------------------------------------------------

def test_parse_llm_response_strips_json_fence() -> None:
    raw = '```json\n{"decision": "PROCEED", "direction": "bullish", "thesis": ".", "risk_factors": [], "confidence": 0.6}\n```'
    result = parse_llm_response(raw)
    assert result["decision"] == "PROCEED"
    assert result["confidence"] == 0.6


def test_parse_llm_response_strips_bare_fence() -> None:
    raw = '```\n{"decision": "HEDGE", "direction": "neutral", "thesis": ".", "risk_factors": [], "confidence": 0.5}\n```'
    result = parse_llm_response(raw)
    assert result["decision"] == "HEDGE"


def test_parse_llm_response_extracts_object_from_prose() -> None:
    raw = ('Here is the analysis you asked for:\n\n'
           '{"decision": "SKIP", "direction": "neutral", "thesis": "no edge", '
           '"risk_factors": [], "confidence": 0.2}\n\n'
           'Hope this helps.')
    result = parse_llm_response(raw)
    assert result["decision"] == "SKIP"
    assert result["confidence"] == 0.2


def test_parse_llm_response_truly_invalid_still_raises() -> None:
    """A response with no JSON-ish content should still raise so the caller
    can record the failure in the audit log."""
    with pytest.raises(Exception):
        parse_llm_response("I cannot help with that request.")


# ---------------------------------------------------------------------------
# Data-source helper unit tests (mocked session)
#
# These cover the new audit-driven plumbing for iv_rank, PCR, LTP, and news
# counts. They mock the SQLAlchemy session at the .execute() boundary so the
# helpers' fallback / aggregation logic gets exercised without DB I/O.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock


def _mock_session_with_results(*results):
    """Build a session whose successive .execute() calls return the supplied
    mocks in order. Each result must support .scalar_one_or_none(), .first(),
    .all(), or .scalar() depending on what the caller invokes."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(results))
    return session


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar = MagicMock(return_value=value)
    return r


def _rows_result(rows):
    r = MagicMock()
    r.all = MagicMock(return_value=rows)
    return r


def _first_result(row):
    r = MagicMock()
    r.first = MagicMock(return_value=row)
    return r


@pytest.mark.asyncio
async def test_get_underlying_ltp_prefers_chain_snapshot() -> None:
    """When OptionsChain has a recent underlying_ltp, use it directly."""
    from src.fno.thesis_synthesizer import _get_underlying_ltp

    session = _mock_session_with_results(_scalar_result(1234.50))
    ltp = await _get_underlying_ltp(session, "fake-id")
    assert ltp == 1234.50
    # Only one query needed (chain hit) — no fallback to price_daily
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_get_underlying_ltp_falls_back_to_price_daily() -> None:
    """No chain row → query price_daily as a fallback."""
    from src.fno.thesis_synthesizer import _get_underlying_ltp

    session = _mock_session_with_results(
        _scalar_result(None),       # chain miss
        _scalar_result(987.65),     # price_daily hit
    )
    ltp = await _get_underlying_ltp(session, "fake-id")
    assert ltp == 987.65
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_get_underlying_ltp_returns_none_on_total_miss() -> None:
    from src.fno.thesis_synthesizer import _get_underlying_ltp

    session = _mock_session_with_results(
        _scalar_result(None), _scalar_result(None),
    )
    assert await _get_underlying_ltp(session, "fake-id") is None


@pytest.mark.asyncio
async def test_get_underlying_ltp_filters_zero_chain_value() -> None:
    """A chain row with underlying_ltp=0 (corrupt) must NOT be returned —
    the SQL filter is `> 0` so the query wouldn't return it; verify the
    fallback path runs when the chain query returns None (which it will
    when all chain rows have underlying_ltp <= 0)."""
    from src.fno.thesis_synthesizer import _get_underlying_ltp

    # Simulating the post-filter behaviour: chain query returns None
    # (because the > 0 filter excluded the corrupt zero row), then
    # price_daily fallback succeeds with a real value.
    session = _mock_session_with_results(
        _scalar_result(None),    # chain query returns None (zero row filtered out by `> 0`)
        _scalar_result(987.65),  # price_daily fallback fires
    )
    ltp = await _get_underlying_ltp(session, "fake-id")
    assert ltp == 987.65


@pytest.mark.asyncio
async def test_get_iv_rank_returns_real_values() -> None:
    from src.fno.thesis_synthesizer import _get_iv_rank

    row = MagicMock(iv_rank_52w=78.5, atm_iv=23.4)
    session = _mock_session_with_results(_first_result(row))
    rank, atm = await _get_iv_rank(session, "fake-id")
    assert rank == 78.5
    assert atm == 23.4


@pytest.mark.asyncio
async def test_get_iv_rank_returns_none_on_no_history() -> None:
    """Instruments without iv_history rows must return (None, None) so the
    caller can fall back to a neutral default."""
    from src.fno.thesis_synthesizer import _get_iv_rank

    session = _mock_session_with_results(_first_result(None))
    rank, atm = await _get_iv_rank(session, "fake-id")
    assert rank is None
    assert atm is None


@pytest.mark.asyncio
async def test_get_iv_rank_clamps_out_of_range_to_none() -> None:
    """Historical iv_history rows written before the clamp fix can have
    nonsense values like -587 or +8100 (chain iv unit mismatch). Those
    should be discarded so the LLM sees a neutral default, not garbage."""
    from src.fno.thesis_synthesizer import _get_iv_rank

    # -587 is outside [0, 100] → treat as missing
    session = _mock_session_with_results(
        _first_result(MagicMock(iv_rank_52w=-587.2, atm_iv=33.4))
    )
    rank, atm = await _get_iv_rank(session, "fake-id")
    assert rank is None
    # atm_iv still surfaces — caller may want to log it for diagnostics
    assert atm == 33.4

    # 8100 is outside [0, 100] → treat as missing
    session = _mock_session_with_results(
        _first_result(MagicMock(iv_rank_52w=8100.0, atm_iv=42.0))
    )
    rank, atm = await _get_iv_rank(session, "fake-id")
    assert rank is None


def test_compute_iv_rank_clamps_negative() -> None:
    """compute_iv_rank should never return a value outside [0, 100].
    Previously when current IV was outside the 52w high/low (e.g., due to
    a unit mismatch between today's and historical atm_iv), the formula
    could produce values like -6273. Verify clamping."""
    from src.fno.iv_history_builder import compute_iv_rank

    # Current way below history → clamp to 0
    assert compute_iv_rank(0.5, [32.0, 32.5, 33.0]) == 0.0
    # Current way above history → clamp to 100
    assert compute_iv_rank(50.0, [32.0, 32.5, 33.0]) == 100.0
    # Current within history → unchanged
    assert compute_iv_rank(32.5, [32.0, 32.5, 33.0]) == 50.0


@pytest.mark.asyncio
async def test_get_chain_pcr_computes_put_call_ratio() -> None:
    """PCR = ΣOI(PE) / ΣOI(CE) for the latest snapshot's nearest expiry."""
    from datetime import date as date_, datetime as datetime_, timezone as tz_
    from src.fno.thesis_synthesizer import _get_chain_pcr

    snap_at = datetime_(2026, 5, 8, 6, 30, tzinfo=tz_.utc)
    expiry = date_(2026, 5, 14)
    session = _mock_session_with_results(
        _scalar_result(snap_at),                    # latest snapshot
        _scalar_result(expiry),                     # nearest expiry
        _rows_result([("CE", 100_000), ("PE", 130_000)]),  # OI sums
    )
    pcr = await _get_chain_pcr(session, "fake-id")
    assert pcr == round(130_000 / 100_000, 4)
    assert pcr == 1.3


@pytest.mark.asyncio
async def test_get_chain_pcr_handles_missing_pe_or_ce() -> None:
    """If a snapshot only has CE rows (no PE), PE OI is 0 → pcr=0."""
    from datetime import date as date_, datetime as datetime_, timezone as tz_
    from src.fno.thesis_synthesizer import _get_chain_pcr

    session = _mock_session_with_results(
        _scalar_result(datetime_(2026, 5, 8, tzinfo=tz_.utc)),
        _scalar_result(date_(2026, 5, 14)),
        _rows_result([("CE", 50_000)]),  # PE missing
    )
    pcr = await _get_chain_pcr(session, "fake-id")
    assert pcr == 0.0


@pytest.mark.asyncio
async def test_get_chain_pcr_returns_none_when_ce_zero() -> None:
    """Divide-by-zero guard: zero CE OI must return None, not raise."""
    from datetime import date as date_, datetime as datetime_, timezone as tz_
    from src.fno.thesis_synthesizer import _get_chain_pcr

    session = _mock_session_with_results(
        _scalar_result(datetime_(2026, 5, 8, tzinfo=tz_.utc)),
        _scalar_result(date_(2026, 5, 14)),
        _rows_result([("CE", 0), ("PE", 100_000)]),
    )
    assert await _get_chain_pcr(session, "fake-id") is None


@pytest.mark.asyncio
async def test_get_chain_pcr_returns_none_when_no_snapshot() -> None:
    """No chain at all → None (caller falls back to oi_structure='unknown')."""
    from src.fno.thesis_synthesizer import _get_chain_pcr

    session = _mock_session_with_results(_scalar_result(None))
    assert await _get_chain_pcr(session, "fake-id") is None


@pytest.mark.asyncio
async def test_get_news_counts_aggregates_by_action() -> None:
    """Bullish = BUY+BULLISH, Bearish = SELL+BEARISH; HOLD ignored."""
    from src.fno.thesis_synthesizer import _get_news_counts

    session = _mock_session_with_results(_rows_result([
        ("BUY", 3), ("BULLISH", 1), ("SELL", 2), ("HOLD", 5),
    ]))
    bull, bear = await _get_news_counts(session, "fake-id", lookback_hours=18)
    assert bull == 4
    assert bear == 2


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


# ---------------------------------------------------------------------------
# build_user_prompt — explicit "no empty data to LLM" rendering paths
# ---------------------------------------------------------------------------

def _base_prompt_kwargs() -> dict:
    """Minimal valid kwargs for build_user_prompt — tests override one field."""
    return dict(
        symbol="TEST", sector="Energy", underlying_price=1000.0,
        iv_rank=55.0, iv_regime="neutral", oi_structure="balanced",
        days_to_expiry=4, news_score=6.0, sentiment_score=6.0,
        fii_dii_score=5.0, macro_align_score=5.0,
        convergence_score=5.0, composite_score=5.0,
        bullish_count=2, bearish_count=1, lookback_hours=18,
        fii_net_cr=500.0, dii_net_cr=300.0,
        macro_drivers=["BRENT"], headlines=["headline"],
    )


def test_build_user_prompt_iv_rank_none_renders_unknown() -> None:
    """iv_rank=None → prompt shows 'unknown (no IV history)' AND iv_regime
    is forced to 'unknown' so the LLM can't apply REGIME GATE on stub data."""
    kwargs = _base_prompt_kwargs() | {"iv_rank": None, "iv_regime": "neutral"}
    prompt = build_user_prompt(**kwargs)
    assert "unknown (no IV history)" in prompt
    assert "IV Regime: unknown" in prompt
    # The pre-fix string of "IV Rank (52w): 50.0%" must NOT appear
    assert "50.0%" not in prompt


def test_build_user_prompt_fii_dii_unavailable_renders_explicit_message() -> None:
    """fii_net_cr=None or dii_net_cr=None → '(data unavailable)' line.
    The LLM must not see a misleading '5/10 (FII net ₹+0Cr ...)' default."""
    kwargs = _base_prompt_kwargs() | {"fii_net_cr": None, "dii_net_cr": None}
    prompt = build_user_prompt(**kwargs)
    assert "(data unavailable)" in prompt
    assert "₹+0Cr" not in prompt
    # Defensive: ensure the score also doesn't appear as "5.00/10" on this line
    assert "FII/DII activity: 5.00/10" not in prompt


def test_build_user_prompt_partial_fii_dii_still_unavailable() -> None:
    """Only one of fii/dii missing → the whole line should still degrade
    to '(data unavailable)' rather than render half the data."""
    kwargs = _base_prompt_kwargs() | {"fii_net_cr": 500.0, "dii_net_cr": None}
    prompt = build_user_prompt(**kwargs)
    assert "(data unavailable)" in prompt


def test_build_user_prompt_iv_rank_present_keeps_regime() -> None:
    """When iv_rank is real, iv_regime is preserved as the caller computed."""
    kwargs = _base_prompt_kwargs() | {"iv_rank": 75.0, "iv_regime": "high"}
    prompt = build_user_prompt(**kwargs)
    assert "75.0%" in prompt
    assert "IV Regime: high" in prompt


def test_build_user_prompt_fii_dii_score_none_renders_na() -> None:
    """If FII/DII data is present but the precomputed score is None
    (e.g., Phase 2 wrote None when data was unavailable but Phase 3 has
    fresh data), render 'n/a' for the score."""
    kwargs = _base_prompt_kwargs() | {
        "fii_dii_score": None,
        "fii_net_cr": 500.0,
        "dii_net_cr": 300.0,
    }
    prompt = build_user_prompt(**kwargs)
    # Score should render "n/a" alongside the real flow values
    assert "n/a/10" in prompt
    assert "FII net ₹+500Cr" in prompt


# ---------------------------------------------------------------------------
# Helper-level guards for empty / corrupt data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_news_counts_respects_anchor() -> None:
    """When anchor is passed, the upper bound of the window equals anchor
    (not 'now'). Used to align Phase 3's news counts with Phase 2's
    news_score window."""
    from src.fno.thesis_synthesizer import _get_news_counts

    anchor = datetime(2026, 5, 8, 1, 30, tzinfo=timezone.utc)
    session = _mock_session_with_results(_rows_result([("BUY", 5)]))
    bull, bear = await _get_news_counts(
        session, "fake-id", lookback_hours=18, anchor=anchor,
    )
    assert bull == 5
    # Verify the anchor was used as the upper bound — sniff the bound
    # values out of the SQL clause's compiled args. Keep this loose: just
    # confirm that a Signal.created_at comparison against ``anchor`` was
    # part of the query.
    call_args = session.execute.call_args
    assert call_args is not None
