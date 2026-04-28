"""Tests for src/runday/checks/chain.py."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runday.checks.base import Severity
from src.runday.checks.chain import (
    ChainCollectionHealthCheck,
    OpenIssuesCheck,
    SourceHealthCheck,
)
from src.runday.config import RundaySettings


@pytest.fixture
def settings() -> RundaySettings:
    return RundaySettings(
        runday_min_chain_nse_share_pct=80.0,
        runday_max_tier1_latency_ms_p95=3000,
        runday_max_tier2_latency_ms_p95=5000,
        runday_max_acceptable_missed_pct=5.0,
    )


# ---------------------------------------------------------------------------
# ChainCollectionHealthCheck
# ---------------------------------------------------------------------------

def _make_chain_session(status_rows, nse_count, tier1_p95, tier2_p95):
    """Build a session mock for chain health check (4 execute calls)."""
    call_idx = [0]
    results_seq = [status_rows, nse_count, tier1_p95, tier2_p95]

    async def _execute(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        mock_result = MagicMock()
        val = results_seq[idx] if idx < len(results_seq) else None

        if isinstance(val, list):
            mock_result.fetchall.return_value = val
        else:
            mock_result.fetchall.return_value = []
            mock_result.scalar.return_value = val
            mock_result.scalar_one_or_none.return_value = val
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = _execute

    @asynccontextmanager
    async def _scope():
        yield mock_session

    return _scope


@pytest.mark.asyncio
async def test_chain_health_all_ok(settings):
    status_rows = [
        ("ok", 170, 1500.0),
        ("fallback_used", 10, 2000.0),
        ("missed", 4, None),
    ]
    session_ctx = _make_chain_session(status_rows, nse_count=170, tier1_p95=1500.0, tier2_p95=2000.0)

    with patch("src.runday.checks.chain.session_scope", session_ctx):
        check = ChainCollectionHealthCheck(settings, lookback_minutes=10)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["total"] == 184
    assert result.details["nse_share_pct"] > 80


@pytest.mark.asyncio
async def test_chain_health_high_missed(settings):
    status_rows = [
        ("ok", 150, 1500.0),
        ("fallback_used", 10, 2000.0),
        ("missed", 40, None),  # >5% missed
    ]
    session_ctx = _make_chain_session(status_rows, nse_count=150, tier1_p95=1500.0, tier2_p95=2000.0)

    with patch("src.runday.checks.chain.session_scope", session_ctx):
        check = ChainCollectionHealthCheck(settings, lookback_minutes=10)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "missed rate" in result.message.lower()


@pytest.mark.asyncio
async def test_chain_health_low_nse_share(settings):
    status_rows = [
        ("ok", 180, 1500.0),
        ("fallback_used", 4, 2000.0),
        ("missed", 0, None),
    ]
    session_ctx = _make_chain_session(status_rows, nse_count=100, tier1_p95=1500.0, tier2_p95=2000.0)

    with patch("src.runday.checks.chain.session_scope", session_ctx):
        check = ChainCollectionHealthCheck(settings, lookback_minutes=10)
        result = await check.run()

    # NSE share ~54% below 80% → WARN
    assert result.severity in (Severity.WARN, Severity.FAIL)


@pytest.mark.asyncio
async def test_chain_health_no_data(settings):
    status_rows: list = []
    session_ctx = _make_chain_session(status_rows, nse_count=0, tier1_p95=None, tier2_p95=None)

    with patch("src.runday.checks.chain.session_scope", session_ctx):
        check = ChainCollectionHealthCheck(settings, lookback_minutes=10)
        result = await check.run()

    assert result.severity == Severity.WARN
    assert "No chain collection" in result.message


# ---------------------------------------------------------------------------
# SourceHealthCheck
# ---------------------------------------------------------------------------

def _make_source_health_session(source_rows):
    mock_result = MagicMock()
    mock_result.scalars.return_value = source_rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope():
        yield mock_session

    return _scope


@pytest.mark.asyncio
async def test_source_health_all_healthy(settings):
    sources = []
    for name in ("nse", "dhan", "angel_one"):
        s = MagicMock()
        s.source = name
        s.status = "healthy"
        s.consecutive_errors = 0
        s.last_error_at = None
        s.last_error = None
        sources.append(s)

    with patch("src.runday.checks.chain.session_scope", _make_source_health_session(sources)):
        check = SourceHealthCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert "3 sources healthy" in result.message


@pytest.mark.asyncio
async def test_source_health_degraded(settings):
    sources = []
    for name, status in (("nse", "degraded"), ("dhan", "healthy"), ("angel_one", "healthy")):
        s = MagicMock()
        s.source = name
        s.status = status
        s.consecutive_errors = 5 if status == "degraded" else 0
        s.last_error_at = None
        s.last_error = "timeout"
        sources.append(s)

    with patch("src.runday.checks.chain.session_scope", _make_source_health_session(sources)):
        check = SourceHealthCheck(settings)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "nse" in result.message


# ---------------------------------------------------------------------------
# OpenIssuesCheck
# ---------------------------------------------------------------------------

def _make_issues_session(issue_rows):
    mock_result = MagicMock()
    mock_result.fetchall.return_value = issue_rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope():
        yield mock_session

    return _scope


@pytest.mark.asyncio
async def test_open_issues_none(settings):
    with patch("src.runday.checks.chain.session_scope", _make_issues_session([])):
        check = OpenIssuesCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["total"] == 0


@pytest.mark.asyncio
async def test_open_issues_present(settings):
    rows = [("schema_mismatch", 2), ("sustained_failure", 1)]
    with patch("src.runday.checks.chain.session_scope", _make_issues_session(rows)):
        check = OpenIssuesCheck(settings)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert result.details["total"] == 3
    assert result.details["by_type"]["schema_mismatch"] == 2
