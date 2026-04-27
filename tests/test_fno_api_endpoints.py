"""Integration tests for F&O API endpoints using FastAPI TestClient.

All DB calls are patched so no database is required.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.api.routes.fno import router
from src.api.schemas.fno import (
    FNOBanListResponse,
    FNOCandidateResponse,
    IVHistoryResponse,
    VIXTickResponse,
)

# Build a minimal FastAPI app with just the F&O router (no lifespan/scheduler)
_app = FastAPI()
_app.include_router(router)
_client = TestClient(_app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Mock data factories
# ---------------------------------------------------------------------------

def _inst_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


def _cand_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000002")


def _mock_candidate():
    cand = MagicMock()
    cand.id = _cand_id()
    cand.instrument_id = _inst_id()
    cand.run_date = date(2026, 4, 27)
    cand.phase = 2
    cand.passed_liquidity = True
    cand.atm_oi = 75000
    cand.atm_spread_pct = Decimal("0.003")
    cand.avg_volume_5d = 1_500_000
    cand.news_score = Decimal("7.5")
    cand.sentiment_score = Decimal("6.0")
    cand.fii_dii_score = Decimal("8.0")
    cand.macro_align_score = Decimal("7.0")
    cand.convergence_score = Decimal("7.5")
    cand.composite_score = Decimal("7.2")
    cand.technical_pass = None
    cand.iv_regime = "low"
    cand.oi_structure = None
    cand.llm_thesis = None
    cand.llm_decision = None
    cand.symbol = None  # populated by route after model_validate
    cand.config_version = "v1"
    cand.created_at = datetime(2026, 4, 27, 7, 0, tzinfo=timezone.utc)
    return cand


def _mock_iv_history():
    row = MagicMock()
    row.instrument_id = _inst_id()
    row.date = date(2026, 4, 27)
    row.atm_iv = Decimal("0.2150")
    row.iv_rank_52w = Decimal("35.50")
    row.iv_percentile_52w = Decimal("42.00")
    return row


def _mock_vix_tick():
    row = MagicMock()
    row.timestamp = datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)
    row.vix_value = Decimal("14.25")
    row.regime = "neutral"
    return row


def _mock_ban():
    row = MagicMock()
    row.symbol = "ZOMATO"
    row.ban_date = date(2026, 4, 27)
    row.is_active = True
    return row


def _make_session_scope(rows_per_call: list[list]):
    """Return a patched session_scope that yields results in order."""
    call_idx = 0
    rows = list(rows_per_call)

    @asynccontextmanager
    async def _scope():
        nonlocal call_idx
        session = AsyncMock()
        current = rows[call_idx] if call_idx < len(rows) else []
        call_idx += 1

        result_mock = MagicMock()
        result_mock.all.return_value = current
        result_mock.scalars.return_value = MagicMock(all=lambda: current)
        result_mock.scalar_one_or_none.return_value = current[0] if current else None
        result_mock.first.return_value = current[0] if current else None
        session.execute = AsyncMock(return_value=result_mock)
        yield session

    return _scope


# ---------------------------------------------------------------------------
# GET /fno/candidates
# ---------------------------------------------------------------------------

def test_list_candidates_empty() -> None:
    scope = _make_session_scope([[]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/candidates")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_candidates_returns_results() -> None:
    cand = _mock_candidate()
    scope = _make_session_scope([[(cand, "NIFTY")]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/candidates")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["symbol"] == "NIFTY"
    assert data[0]["phase"] == 2


def test_list_candidates_filter_by_phase() -> None:
    scope = _make_session_scope([[]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/candidates?phase=1")
    assert resp.status_code == 200


def test_list_candidates_filter_passed_only() -> None:
    scope = _make_session_scope([[]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/candidates?passed_only=true")
    assert resp.status_code == 200


def test_list_candidates_phase_validation() -> None:
    resp = _client.get("/fno/candidates?phase=99")
    assert resp.status_code == 422  # FastAPI validation error


def test_list_candidates_limit_validation() -> None:
    resp = _client.get("/fno/candidates?limit=999")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /fno/candidates/{id}
# ---------------------------------------------------------------------------

def test_get_candidate_found() -> None:
    cand = _mock_candidate()
    scope = _make_session_scope([[(cand, "NIFTY")]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get(f"/fno/candidates/{_cand_id()}")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "NIFTY"


def test_get_candidate_not_found() -> None:
    scope = _make_session_scope([[]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get(f"/fno/candidates/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_candidate_invalid_uuid() -> None:
    resp = _client.get("/fno/candidates/not-a-uuid")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /fno/iv-history/{instrument_id}
# ---------------------------------------------------------------------------

def test_get_iv_history_returns_rows() -> None:
    row = _mock_iv_history()
    scope = _make_session_scope([[row]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get(f"/fno/iv-history/{_inst_id()}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert float(data[0]["atm_iv"]) == pytest.approx(0.2150)


def test_get_iv_history_empty() -> None:
    scope = _make_session_scope([[]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get(f"/fno/iv-history/{_inst_id()}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_iv_history_limit_validation() -> None:
    resp = _client.get(f"/fno/iv-history/{_inst_id()}?limit=999")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /fno/vix
# ---------------------------------------------------------------------------

def test_get_vix_returns_rows() -> None:
    row = _mock_vix_tick()
    scope = _make_session_scope([[row]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/vix")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["regime"] == "neutral"


def test_get_vix_empty() -> None:
    scope = _make_session_scope([[]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/vix")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /fno/ban-list
# ---------------------------------------------------------------------------

def test_get_ban_list_returns_active() -> None:
    row = _mock_ban()
    scope = _make_session_scope([[row]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/ban-list")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["symbol"] == "ZOMATO"
    assert data[0]["is_active"] is True


def test_get_ban_list_empty() -> None:
    scope = _make_session_scope([[]])
    with patch("src.api.routes.fno.session_scope", scope):
        resp = _client.get("/fno/ban-list")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /fno/pipeline/trigger
# ---------------------------------------------------------------------------

def test_trigger_pipeline_module_disabled() -> None:
    from src.fno import orchestrator
    with patch.object(orchestrator._settings, "fno_module_enabled", False):
        resp = _client.post("/fno/pipeline/trigger")
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped_module_disabled"


def test_trigger_pipeline_runs_pipeline() -> None:
    from src.fno import orchestrator

    mock_result = {
        "run_date": "2026-04-27",
        "phase1_total": 50,
        "phase1_passed": 40,
        "phase2_total": 40,
        "phase2_passed": 15,
        "phase3_total": 15,
        "phase3_proceed": 5,
    }

    with patch(
        "src.fno.orchestrator.run_premarket_pipeline",
        AsyncMock(return_value=mock_result),
    ):
        resp = _client.post("/fno/pipeline/trigger?run_date=2026-04-27")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["phase1_passed"] == 40
    assert data["phase3_proceed"] == 5
