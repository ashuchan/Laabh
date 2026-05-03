"""Tests for Task 2 — FII/DII archive routing in fii_dii_collector."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collectors.fii_dii_collector import _parse_fii_dii, fetch_yesterday


def test_parse_fii_dii_basic():
    records = [
        {"category": "FII/FPI", "buyValue": 1000, "sellValue": 700, "date": "23-Apr-2026"},
        {"category": "DII", "buyValue": 500, "sellValue": 300, "date": "23-Apr-2026"},
    ]
    result = _parse_fii_dii(records)
    assert result["fii_net_cr"] == pytest.approx(300.0)
    assert result["dii_net_cr"] == pytest.approx(200.0)
    assert result["date"] == "23-Apr-2026"


@pytest.mark.asyncio
async def test_fetch_yesterday_routes_to_archive_for_historical():
    """fetch_yesterday with a past date calls _fetch_fii_dii_archive."""
    past_date = date.today() - timedelta(days=10)
    fake_source = MagicMock()
    fake_source.id = "src-1"

    archive_records = [
        {"category": "FII/FPI", "buyValue": 800, "sellValue": 600, "date": "13-Apr-2026"},
    ]

    with (
        patch("src.collectors.fii_dii_collector._fetch_fii_dii_archive", new=AsyncMock(return_value=archive_records)) as mock_archive,
        patch("src.collectors.fii_dii_collector._fetch_fii_dii_raw", new=AsyncMock(return_value=[])) as mock_live,
        patch("src.collectors.fii_dii_collector.session_scope") as mock_scope,
    ):
        session_mock = AsyncMock()
        source_result = MagicMock()
        source_result.scalar_one_or_none.return_value = fake_source
        session_mock.execute = AsyncMock(return_value=source_result)
        session_mock.add = MagicMock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        summary = await fetch_yesterday(target_date=past_date)

    assert mock_archive.called
    assert not mock_live.called
    assert summary is not None
    assert summary["fii_net_cr"] == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_fetch_yesterday_uses_live_for_today():
    """fetch_yesterday with today's date calls live API."""
    today = date.today()
    fake_source = MagicMock()
    fake_source.id = "src-1"

    live_records = [
        {"category": "FII/FPI", "buyValue": 500, "sellValue": 400, "date": "today"},
    ]

    with (
        patch("src.collectors.fii_dii_collector._fetch_fii_dii_archive", new=AsyncMock(return_value=[])) as mock_archive,
        patch("src.collectors.fii_dii_collector._fetch_fii_dii_raw", new=AsyncMock(return_value=live_records)) as mock_live,
        patch("src.collectors.fii_dii_collector.session_scope") as mock_scope,
    ):
        session_mock = AsyncMock()
        source_result = MagicMock()
        source_result.scalar_one_or_none.return_value = fake_source
        session_mock.execute = AsyncMock(return_value=source_result)
        session_mock.add = MagicMock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        await fetch_yesterday(target_date=today)

    assert mock_live.called
    assert not mock_archive.called
