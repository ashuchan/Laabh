"""Tests for Task 2 — as_of parameter on macro_collector."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collectors.macro_collector import collect


@pytest.mark.asyncio
async def test_collect_as_of_stamps_fetched_at():
    """collect() with as_of= stamps RawContent.fetched_at with that datetime."""
    as_of = datetime(2026, 4, 23, 7, 0, 0, tzinfo=timezone.utc)
    added_rows = []

    fake_source = MagicMock()
    fake_source.id = "src-1"

    with (
        patch("src.collectors.macro_collector._fetch_ticker_historical") as mock_hist,
        patch("src.collectors.macro_collector.session_scope") as mock_scope,
    ):
        mock_hist.return_value = {"symbol": "BZ=F", "price": 82.5, "prev_close": 81.0, "change_pct": None}
        session_mock = AsyncMock()
        source_result = MagicMock()
        source_result.scalar_one_or_none.return_value = fake_source
        session_mock.execute = AsyncMock(return_value=source_result)
        session_mock.add = lambda row: added_rows.append(row)
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        count = await collect(as_of=as_of)

    assert count > 0
    fetched_ats = [r.fetched_at for r in added_rows if hasattr(r, "fetched_at")]
    assert all(fa == as_of for fa in fetched_ats)


@pytest.mark.asyncio
async def test_collect_live_uses_live_fetch():
    """collect() without as_of uses _fetch_ticker (live)."""
    fake_source = MagicMock()
    fake_source.id = "src-1"

    with (
        patch("src.collectors.macro_collector._fetch_ticker") as mock_live,
        patch("src.collectors.macro_collector._fetch_ticker_historical") as mock_hist,
        patch("src.collectors.macro_collector.session_scope") as mock_scope,
    ):
        mock_live.return_value = {"symbol": "BZ=F", "price": 82.5, "prev_close": 81.0, "change_pct": None}
        session_mock = AsyncMock()
        source_result = MagicMock()
        source_result.scalar_one_or_none.return_value = fake_source
        session_mock.execute = AsyncMock(return_value=source_result)
        session_mock.add = MagicMock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        await collect()

    assert mock_live.called
    assert not mock_hist.called
