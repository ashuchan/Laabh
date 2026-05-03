"""Tests for Task 6 — replay preflight profile."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runday.checks.data import BhavcopyAvailableCheck


@pytest.mark.asyncio
async def test_bhavcopy_available_check_ok():
    """BhavcopyAvailableCheck returns OK when bhavcopy is available."""
    from src.runday.checks.base import Severity

    settings = MagicMock()
    check = BhavcopyAvailableCheck(settings, date(2026, 4, 23))

    with patch(
        "src.runday.checks.data.fetch_fo_bhavcopy",
        new=AsyncMock(return_value=MagicMock()),
    ):
        result = await check.run()

    assert result.severity == Severity.OK
    assert "2026-04-23" in result.message


@pytest.mark.asyncio
async def test_bhavcopy_available_check_fail_on_missing():
    """BhavcopyAvailableCheck returns FAIL when bhavcopy 404."""
    from src.dryrun.bhavcopy import BhavcopyMissingError
    from src.runday.checks.base import Severity

    settings = MagicMock()
    check = BhavcopyAvailableCheck(settings, date(2026, 4, 23))

    with patch(
        "src.runday.checks.data.fetch_fo_bhavcopy",
        new=AsyncMock(side_effect=BhavcopyMissingError("NSE archive 404")),
    ):
        result = await check.run()

    assert result.severity == Severity.FAIL


@pytest.mark.asyncio
async def test_preflight_replay_profile_uses_bhavcopy_check():
    """_preflight_async with profile=replay includes BhavcopyAvailableCheck."""
    from src.runday.cli import _preflight_async

    with (
        patch("src.runday.cli.DBConnectivityCheck") as mock_db,
        patch("src.runday.cli.MigrationsCurrentCheck") as mock_mig,
        patch("src.runday.cli.RequiredTablesCheck") as mock_rt,
        patch("src.runday.cli.AnthropicCheck") as mock_ant,
        patch("src.runday.cli.TradingDayCheck") as mock_td,
        patch("src.runday.cli.BhavcopyAvailableCheck") as mock_bhav,
        patch("src.runday.cli.TelegramReporter") as mock_tg,
        patch("src.runday.cli.get_runday_settings") as mock_settings,
    ):
        from src.runday.checks.base import CheckResult, Severity

        ok_result = CheckResult(name="x", severity=Severity.OK, message="ok")
        for mock_cls in [mock_db, mock_mig, mock_rt, mock_ant, mock_td, mock_bhav]:
            instance = MagicMock()
            instance.name = "preflight.x"
            instance.run = AsyncMock(return_value=ok_result)
            mock_cls.return_value = instance

        mock_settings.return_value = MagicMock(runday_telegram_on_preflight_ok=False)
        mock_tg.return_value = MagicMock(send_preflight_ok=AsyncMock(), send_preflight_fail=AsyncMock())

        code = await _preflight_async(
            quiet=True,
            emit_json=False,
            skip=set(),
            profile="replay",
            target_date=date(2026, 4, 23),
        )

    assert mock_bhav.called
    assert code == 0
