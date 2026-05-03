"""Tests for Task 7 — Phase4ManageCheck with simulated now."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

from src.runday.checks.pipeline import Phase4ManageCheck

_IST = pytz.timezone("Asia/Kolkata")


def _utc(d: date, hour: int, minute: int) -> datetime:
    """Construct a UTC datetime from a date and IST time."""
    ist_dt = _IST.localize(datetime(d.year, d.month, d.day, hour, minute, 0))
    return ist_dt.astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_phase4_manage_outside_market_hours_warns():
    """now before 09:15 IST → WARN (skipped), no DB call."""
    from src.runday.checks.base import Severity

    D = date(2026, 4, 23)
    now = _utc(D, 8, 0)  # 08:00 IST — before market open

    check = Phase4ManageCheck(None, D, now=now)
    result = await check.run()

    assert result.severity == Severity.WARN
    assert "Outside market hours" in result.message


@pytest.mark.asyncio
async def test_phase4_manage_inside_market_hours_ok():
    """now during market hours with a recent job_log row → OK."""
    from src.runday.checks.base import CheckResult, Severity

    D = date(2026, 4, 23)
    now = _utc(D, 10, 30)  # 10:30 IST — well inside

    last_run_utc = now  # pretend the manage loop just ran

    with patch("src.runday.checks.pipeline.session_scope") as mock_scope:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = last_run_utc.replace(tzinfo=None)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        check = Phase4ManageCheck(None, D, now=now)
        result = await check.run()

    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_phase4_manage_inside_market_hours_fail_if_stale():
    """now during market hours but no recent job_log row → FAIL."""
    from src.runday.checks.base import Severity

    D = date(2026, 4, 23)
    now = _utc(D, 11, 0)  # 11:00 IST

    with patch("src.runday.checks.pipeline.session_scope") as mock_scope:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        check = Phase4ManageCheck(None, D, now=now)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "5 minutes" in result.message


@pytest.mark.asyncio
async def test_phase4_manage_derives_today_from_now():
    """When anchor_date is None, today is derived from now.date()."""
    from src.runday.checks.base import Severity

    D = date(2026, 4, 23)
    now = _utc(D, 12, 0)  # 12:00 IST, no anchor_date passed

    with patch("src.runday.checks.pipeline.session_scope") as mock_scope:
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        # No anchor_date — should use now.date()
        check = Phase4ManageCheck(None, now=now)
        result = await check.run()

    # Should have attempted the DB query (not WARN), meaning market hours computed from now.date()
    assert result.severity == Severity.FAIL


@pytest.mark.asyncio
async def test_phase4_manage_after_1430_warns():
    """now after 14:30 IST → WARN."""
    from src.runday.checks.base import Severity

    D = date(2026, 4, 23)
    now = _utc(D, 15, 0)  # 15:00 IST — after manage window

    check = Phase4ManageCheck(None, D, now=now)
    result = await check.run()

    assert result.severity == Severity.WARN
