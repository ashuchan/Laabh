"""Tests for src/runday/checks/trading.py."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runday.checks.base import Severity
from src.runday.checks.trading import RiskCapCheck, TradingStatusCheck
from src.runday.config import RundaySettings

TODAY = date(2026, 4, 27)


@pytest.fixture
def settings() -> RundaySettings:
    return RundaySettings(fno_phase4_max_open_positions=3)


def _make_trading_session(*scalar_values):
    """Session returning successive scalar values for successive execute() calls."""
    call_idx = [0]
    seq = list(scalar_values)

    async def _execute(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        mock_result = MagicMock()
        if idx < len(seq):
            val = seq[idx]
            if isinstance(val, list):
                mock_result.fetchall.return_value = val
            else:
                mock_result.scalar.return_value = val
                mock_result.scalar_one_or_none.return_value = val
        else:
            mock_result.fetchall.return_value = []
            mock_result.scalar.return_value = 0
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = _execute

    @asynccontextmanager
    async def _scope():
        yield mock_session

    return _scope


# ---------------------------------------------------------------------------
# TradingStatusCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trading_status_normal(settings):
    status_rows = [
        ("proposed", 4),
        ("paper_filled", 3),
        ("closed_target", 1),
    ]
    session_ctx = _make_trading_session(status_rows, 2140.0)

    with patch("src.runday.checks.trading.session_scope", session_ctx):
        check = TradingStatusCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["day_pnl"] == 2140.0
    assert result.details["open_positions"] == 3


@pytest.mark.asyncio
async def test_trading_status_risk_breach(settings):
    # 4 filled positions > max 3
    status_rows = [
        ("paper_filled", 4),
    ]
    session_ctx = _make_trading_session(status_rows, 0.0)

    with patch("src.runday.checks.trading.session_scope", session_ctx):
        check = TradingStatusCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "RISK BREACH" in result.message


@pytest.mark.asyncio
async def test_trading_status_empty_day(settings):
    session_ctx = _make_trading_session([], 0.0)

    with patch("src.runday.checks.trading.session_scope", session_ctx):
        check = TradingStatusCheck(settings, anchor_date=TODAY)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["proposed"] == 0


# ---------------------------------------------------------------------------
# RiskCapCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_risk_cap_ok(settings):
    session_ctx = _make_trading_session(2)

    with patch("src.runday.checks.trading.session_scope", session_ctx):
        check = RiskCapCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert "2/3" in result.message


@pytest.mark.asyncio
async def test_risk_cap_breached(settings):
    session_ctx = _make_trading_session(5)

    with patch("src.runday.checks.trading.session_scope", session_ctx):
        check = RiskCapCheck(settings)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "5 open positions > max 3" in result.message


@pytest.mark.asyncio
async def test_risk_cap_zero_positions(settings):
    session_ctx = _make_trading_session(0)

    with patch("src.runday.checks.trading.session_scope", session_ctx):
        check = RiskCapCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert "0/3" in result.message
