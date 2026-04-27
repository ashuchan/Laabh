"""Tests for F&O API schemas — pure Pydantic validation."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.api.schemas.fno import (
    FNOBanListResponse,
    FNOCandidateResponse,
    IVHistoryResponse,
    PipelineTriggerResponse,
    VIXTickResponse,
)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# FNOCandidateResponse
# ---------------------------------------------------------------------------

def test_fno_candidate_response_minimal() -> None:
    data = {
        "id": _uuid(),
        "instrument_id": _uuid(),
        "run_date": date.today(),
        "phase": 1,
        "created_at": _now(),
    }
    resp = FNOCandidateResponse(**data)
    assert resp.phase == 1
    assert resp.symbol is None


def test_fno_candidate_response_full() -> None:
    data = {
        "id": _uuid(),
        "instrument_id": _uuid(),
        "symbol": "NIFTY",
        "run_date": date(2026, 4, 27),
        "phase": 2,
        "passed_liquidity": True,
        "atm_oi": 75000,
        "atm_spread_pct": Decimal("0.003"),
        "avg_volume_5d": 1_500_000,
        "news_score": Decimal("7.5"),
        "sentiment_score": Decimal("6.0"),
        "fii_dii_score": Decimal("8.0"),
        "macro_align_score": Decimal("7.0"),
        "convergence_score": Decimal("7.5"),
        "composite_score": Decimal("7.2"),
        "iv_regime": "low",
        "llm_decision": "PROCEED",
        "config_version": "v1",
        "created_at": _now(),
    }
    resp = FNOCandidateResponse(**data)
    assert resp.symbol == "NIFTY"
    assert resp.composite_score == Decimal("7.2")
    assert resp.llm_decision == "PROCEED"


# ---------------------------------------------------------------------------
# IVHistoryResponse
# ---------------------------------------------------------------------------

def test_iv_history_response() -> None:
    resp = IVHistoryResponse(
        instrument_id=_uuid(),
        date=date(2026, 4, 27),
        atm_iv=Decimal("0.2150"),
        iv_rank_52w=Decimal("35.50"),
        iv_percentile_52w=Decimal("42.00"),
    )
    assert float(resp.atm_iv) == pytest.approx(0.215)
    assert resp.iv_rank_52w == Decimal("35.50")


def test_iv_history_response_optional_fields_none() -> None:
    resp = IVHistoryResponse(
        instrument_id=_uuid(),
        date=date(2026, 4, 27),
        atm_iv=Decimal("0.18"),
    )
    assert resp.iv_rank_52w is None
    assert resp.iv_percentile_52w is None


# ---------------------------------------------------------------------------
# VIXTickResponse
# ---------------------------------------------------------------------------

def test_vix_tick_response_low_regime() -> None:
    resp = VIXTickResponse(
        timestamp=_now(),
        vix_value=Decimal("11.50"),
        regime="low",
    )
    assert resp.regime == "low"


def test_vix_tick_response_high_regime() -> None:
    resp = VIXTickResponse(
        timestamp=_now(),
        vix_value=Decimal("22.30"),
        regime="high",
    )
    assert resp.regime == "high"


# ---------------------------------------------------------------------------
# FNOBanListResponse
# ---------------------------------------------------------------------------

def test_fno_ban_list_response() -> None:
    resp = FNOBanListResponse(
        symbol="ZOMATO",
        ban_date=date(2026, 4, 27),
        is_active=True,
    )
    assert resp.symbol == "ZOMATO"
    assert resp.is_active is True


# ---------------------------------------------------------------------------
# PipelineTriggerResponse
# ---------------------------------------------------------------------------

def test_pipeline_trigger_response_ok() -> None:
    resp = PipelineTriggerResponse(
        status="ok",
        run_date="2026-04-27",
        phase1_passed=40,
        phase2_passed=12,
        phase3_proceed=4,
    )
    assert resp.status == "ok"
    assert resp.phase1_passed == 40


def test_pipeline_trigger_response_skipped() -> None:
    resp = PipelineTriggerResponse(
        status="skipped_module_disabled",
        run_date="2026-04-27",
    )
    assert resp.phase1_passed == 0
    assert resp.phase3_proceed == 0
