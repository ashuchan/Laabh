"""Tests for Task 2 — as_of parameter on vix_collector."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from src.fno.vix_collector import run_once


@pytest.mark.asyncio
async def test_run_once_as_of_stamps_row():
    """run_once with as_of= stamps VIXTick.timestamp with that datetime."""
    as_of = datetime(2026, 4, 23, 9, 30, 0, tzinfo=timezone.utc)
    added_rows = []

    with (
        patch("src.fno.vix_collector._fetch_vix_historical", new=AsyncMock(return_value=15.5)),
        patch("src.fno.vix_collector.session_scope") as mock_scope,
    ):
        session_mock = AsyncMock()
        session_mock.add = lambda row: added_rows.append(row)
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        run_id = uuid.uuid4()
        row = await run_once(as_of=as_of, dryrun_run_id=run_id)

    assert row.timestamp == as_of
    assert row.dryrun_run_id == run_id
    assert abs(row.vix_value - 15.5) < 0.01


@pytest.mark.asyncio
async def test_run_once_live_path_unchanged():
    """run_once without as_of uses Angel One and datetime.now."""
    with (
        patch("src.fno.vix_collector._fetch_vix_from_angel_one", new=AsyncMock(return_value=14.0)),
        patch("src.fno.vix_collector.session_scope") as mock_scope,
    ):
        session_mock = AsyncMock()
        session_mock.add = MagicMock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        row = await run_once()

    assert row.vix_value == 14.0
    assert row.dryrun_run_id is None
