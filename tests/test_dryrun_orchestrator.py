"""Tests for Task 8 — dry-run orchestrator.

Uses heavy mocking to avoid any real DB or network calls.
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.dryrun.orchestrator import ReplayGateFailed, ReplayResult, replay


def _ok_check(name: str):
    from src.runday.checks.base import CheckResult, Severity
    mock = MagicMock()
    mock.name = name
    mock.run = AsyncMock(return_value=CheckResult(name=name, severity=Severity.OK, message="ok"))
    return mock


def _fail_check(name: str):
    from src.runday.checks.base import CheckResult, Severity
    mock = MagicMock()
    mock.name = name
    mock.run = AsyncMock(return_value=CheckResult(name=name, severity=Severity.FAIL, message="fail"))
    return mock


@pytest.mark.asyncio
async def test_replay_success_no_telegram_sent():
    """Successful replay never dispatches a real Telegram message."""
    D = date(2026, 4, 23)

    with (
        patch("src.dryrun.orchestrator.DBConnectivityCheck", return_value=_ok_check("preflight.db_connectivity")),
        patch("src.dryrun.orchestrator.RequiredTablesCheck", return_value=_ok_check("preflight.required_tables")),
        patch("src.dryrun.orchestrator.TradingDayCheck", return_value=_ok_check("preflight.trading_day")),
        patch("src.dryrun.orchestrator.BhavcopyAvailableCheck", return_value=_ok_check("preflight.bhavcopy_available")),
        patch("src.dryrun.orchestrator.DhanHistoricalSource"),
        patch("src.dryrun.orchestrator.collect_tier", new=AsyncMock()),
        patch("src.dryrun.orchestrator.collect_vix", new=AsyncMock()),
        patch("src.dryrun.orchestrator.collect_macro", new=AsyncMock()),
        patch("src.fno.ban_list.fetch_today", new=AsyncMock()),
        patch("src.collectors.fii_dii_collector.fetch_yesterday", new=AsyncMock()),
        patch("src.fno.orchestrator.run_premarket_pipeline", new=AsyncMock(return_value={"phase1_passed": 5})),
        patch("src.dryrun.orchestrator.make_phase_check", return_value=_ok_check("checkpoint.phase1")),
        patch("src.fno.intraday_manager.IntradayState"),
        patch("src.fno.orchestrator.run_eod_tasks", new=AsyncMock()),
    ):
        result = await replay(D, mock_llm=True)

    assert result.success is True
    # All Telegrams were suppressed (captured, not sent)
    real_sends = [c for c in result.captures if c.get("type") == "telegram" and "real" in c.get("msg", "")]
    assert len(real_sends) == 0


@pytest.mark.asyncio
async def test_replay_gate_fail_aborts():
    """ReplayGateFailed is raised when a mandatory gate fails."""
    D = date(2026, 4, 23)

    with (
        patch("src.dryrun.orchestrator.DBConnectivityCheck", return_value=_ok_check("preflight.db")),
        patch("src.dryrun.orchestrator.RequiredTablesCheck", return_value=_ok_check("preflight.tables")),
        patch("src.dryrun.orchestrator.TradingDayCheck", return_value=_ok_check("preflight.trading_day")),
        patch("src.dryrun.orchestrator.BhavcopyAvailableCheck", return_value=_fail_check("preflight.bhavcopy_available")),
    ):
        with pytest.raises(ReplayGateFailed, match="bhavcopy_available"):
            await replay(D, mock_llm=True)


@pytest.mark.asyncio
async def test_replay_stamps_run_id():
    """Replay uses the provided run_id."""
    D = date(2026, 4, 23)
    run_id = uuid.uuid4()

    with (
        patch("src.dryrun.orchestrator.DBConnectivityCheck", return_value=_ok_check("preflight.db")),
        patch("src.dryrun.orchestrator.RequiredTablesCheck", return_value=_ok_check("preflight.tables")),
        patch("src.dryrun.orchestrator.TradingDayCheck", return_value=_ok_check("preflight.trading_day")),
        patch("src.dryrun.orchestrator.BhavcopyAvailableCheck", return_value=_ok_check("preflight.bhavcopy")),
        patch("src.dryrun.orchestrator.DhanHistoricalSource"),
        patch("src.dryrun.orchestrator.collect_tier", new=AsyncMock()),
        patch("src.dryrun.orchestrator.collect_vix", new=AsyncMock()),
        patch("src.dryrun.orchestrator.collect_macro", new=AsyncMock()),
        patch("src.fno.ban_list.fetch_today", new=AsyncMock()),
        patch("src.collectors.fii_dii_collector.fetch_yesterday", new=AsyncMock()),
        patch("src.fno.orchestrator.run_premarket_pipeline", new=AsyncMock(return_value={})),
        patch("src.dryrun.orchestrator.make_phase_check", return_value=_ok_check("checkpoint.phase1")),
        patch("src.fno.intraday_manager.IntradayState"),
        patch("src.fno.orchestrator.run_eod_tasks", new=AsyncMock()),
    ):
        result = await replay(D, mock_llm=True, run_id=run_id)

    assert result.run_id == run_id
