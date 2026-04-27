"""Tests for src/runday/checks/audit.py."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runday.checks.audit import LLMAuditCheck, LLMAuditSummaryCheck
from src.runday.checks.base import Severity
from src.runday.config import RundaySettings

TODAY = date(2026, 4, 27)


@pytest.fixture
def settings() -> RundaySettings:
    return RundaySettings(runday_expected_min_phase3_audit_rows=10)


def _make_audit_session(row_data):
    mock_result = MagicMock()
    mock_result.fetchone.return_value = row_data
    mock_result.fetchall.return_value = row_data if isinstance(row_data, list) else []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope():
        yield mock_session

    return _scope


# ---------------------------------------------------------------------------
# LLMAuditCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_audit_pass(settings):
    # (count, p50, p95, p99, tokens_in, tokens_out)
    row = (10, 1200.0, 2500.0, 3000.0, 15000, 5000)
    with patch("src.runday.checks.audit.session_scope", _make_audit_session(row)):
        check = LLMAuditCheck(settings, caller="fno.thesis", anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["row_count"] == 10
    assert result.details["total_tokens_in"] == 15000


@pytest.mark.asyncio
async def test_llm_audit_insufficient_rows(settings):
    row = (5, 1200.0, 2500.0, 3000.0, 7500, 2500)
    with patch("src.runday.checks.audit.session_scope", _make_audit_session(row)):
        check = LLMAuditCheck(settings, caller="fno.thesis", anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert result.details["row_count"] == 5


@pytest.mark.asyncio
async def test_llm_audit_no_rows(settings):
    with patch("src.runday.checks.audit.session_scope", _make_audit_session(None)):
        check = LLMAuditCheck(settings, caller="fno.thesis", anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.WARN
    assert "No LLM audit rows" in result.message


@pytest.mark.asyncio
async def test_llm_audit_null_latency(settings):
    row = (12, None, None, None, 18000, 6000)
    with patch("src.runday.checks.audit.session_scope", _make_audit_session(row)):
        check = LLMAuditCheck(settings, caller="fno.thesis", anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["latency_p50_ms"] is None


# ---------------------------------------------------------------------------
# LLMAuditSummaryCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_summary_empty_day(settings):
    with patch("src.runday.checks.audit.session_scope", _make_audit_session([])):
        check = LLMAuditSummaryCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert "No LLM calls" in result.message


@pytest.mark.asyncio
async def test_llm_summary_with_callers(settings):
    rows = [
        ("fno.thesis", 10, 1200.0, 15000, 5000, 2500.0),
        ("signal.extractor", 5, 800.0, 7500, 2500, 1500.0),
    ]
    with patch("src.runday.checks.audit.session_scope", _make_audit_session(rows)):
        check = LLMAuditSummaryCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["total_rows"] == 15
    assert result.details["total_tokens_in"] == 22500
    assert len(result.details["callers"]) == 2
    assert result.details["estimated_cost_usd"] > 0
