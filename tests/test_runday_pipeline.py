"""Tests for src/runday/checks/pipeline.py."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runday.checks.base import Severity
from src.runday.checks.data import BanListCheck
from src.runday.checks.pipeline import (
    HardExitCheck,
    MorningBriefCheck,
    Phase1Check,
    Phase2Check,
    Phase3Check,
    ReviewLoopCheck,
    TierRefreshCheck,
    make_phase_check,
)
from src.runday.config import RundaySettings

TODAY = date(2026, 4, 27)


@pytest.fixture
def settings() -> RundaySettings:
    return RundaySettings(
        fno_phase2_target_output=20,
        fno_phase3_target_output=10,
        runday_min_phase1_candidates=30,
        runday_expected_min_phase3_audit_rows=10,
    )


def _scalar_session(return_value):
    mock_result = MagicMock()
    mock_result.scalar.return_value = return_value
    mock_result.scalar_one_or_none.return_value = return_value
    mock_result.fetchone.return_value = return_value

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope():
        yield mock_session

    return _scope


def _multi_scalar_session(*values):
    """Session that returns successive scalars for successive execute() calls."""
    results = list(values)
    call_idx = [0]

    async def _execute(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        mock_result = MagicMock()
        val = results[idx] if idx < len(results) else None
        mock_result.scalar.return_value = val
        mock_result.scalar_one_or_none.return_value = val
        mock_result.fetchone.return_value = val
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = _execute

    @asynccontextmanager
    async def _scope():
        yield mock_session

    return _scope


# ---------------------------------------------------------------------------
# TierRefreshCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tier_refresh_pass(settings):
    recent_ts = datetime(2026, 4, 27, 7, 0, 0, tzinfo=timezone.utc)

    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(recent_ts)):
        check = TierRefreshCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_tier_refresh_stale(settings):
    stale_ts = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)

    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(stale_ts)):
        check = TierRefreshCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "stale" in result.message.lower()


@pytest.mark.asyncio
async def test_tier_refresh_empty(settings):
    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(None)):
        check = TierRefreshCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL


# ---------------------------------------------------------------------------
# Phase1Check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase1_pass(settings):
    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(47)):
        check = Phase1Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["count"] == 47


@pytest.mark.asyncio
async def test_phase1_fail_zero_rows(settings):
    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(0)):
        check = Phase1Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "0 candidates" in result.message
    assert "required ≥30" in result.message


@pytest.mark.asyncio
async def test_phase1_fail_insufficient(settings):
    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(10)):
        check = Phase1Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL


# ---------------------------------------------------------------------------
# Phase2Check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_pass(settings):
    with patch("src.runday.checks.pipeline.session_scope", _multi_scalar_session(20, 20)):
        check = Phase2Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["total"] == 20
    assert result.details["scored"] == 20


@pytest.mark.asyncio
async def test_phase2_wrong_count(settings):
    with patch("src.runday.checks.pipeline.session_scope", _multi_scalar_session(15, 15)):
        check = Phase2Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "expected 20" in result.message


@pytest.mark.asyncio
async def test_phase2_null_scores(settings):
    with patch("src.runday.checks.pipeline.session_scope", _multi_scalar_session(20, 18)):
        check = Phase2Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "null composite_score" in result.message


# ---------------------------------------------------------------------------
# Phase3Check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase3_pass(settings):
    with patch("src.runday.checks.pipeline.session_scope", _multi_scalar_session(10, 10)):
        check = Phase3Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_phase3_missing_audit_rows(settings):
    with patch("src.runday.checks.pipeline.session_scope", _multi_scalar_session(10, 5)):
        check = Phase3Check(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "llm_audit_log" in result.message


# ---------------------------------------------------------------------------
# MorningBriefCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_brief_pass(settings):
    mock_result = MagicMock()
    mock_result.fetchone.return_value = ("F&O Morning Brief", datetime(2026, 4, 27, 9, 10, 0))
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope():
        yield mock_session

    with patch("src.runday.checks.pipeline.session_scope", _scope):
        check = MorningBriefCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert "Morning brief sent" in result.message


@pytest.mark.asyncio
async def test_morning_brief_not_sent(settings):
    mock_result = MagicMock()
    mock_result.fetchone.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope():
        yield mock_session

    with patch("src.runday.checks.pipeline.session_scope", _scope):
        check = MorningBriefCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL


# ---------------------------------------------------------------------------
# HardExitCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hard_exit_all_closed(settings):
    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(0)):
        check = HardExitCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_hard_exit_positions_still_open(settings):
    with patch("src.runday.checks.pipeline.session_scope", _scalar_session(2)):
        check = HardExitCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "2 position(s)" in result.message


# ---------------------------------------------------------------------------
# make_phase_check
# ---------------------------------------------------------------------------

def test_make_phase_check_valid(settings):
    for phase in ["tier-refresh", "phase1", "phase2", "phase3",
                  "morning-brief", "phase4-entry", "phase4-manage",
                  "hard-exit", "review-loop"]:
        check = make_phase_check(phase, settings, TODAY)
        assert check is not None, f"Expected a check for phase '{phase}'"


def test_make_phase_check_invalid(settings):
    check = make_phase_check("nonexistent-phase", settings, TODAY)
    assert check is None
