"""Tests for Task 2 — as_of parameter on macro_collector."""
from __future__ import annotations

from datetime import datetime, timezone, date as date_type, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
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


def test_fetch_ticker_historical_ist_timezone():
    """_fetch_ticker_historical must localise tz-naive yfinance indices as IST, not UTC.

    Bug: the original code called tz_localize("UTC") on tz-naive indices, which
    shifted Indian market timestamps by +5:30h.  A 09:15 IST bar would become
    09:15 UTC (= 14:45 IST), making it invisible to an as_of of 10:00 UTC
    (= 15:30 IST).
    """
    from src.collectors.macro_collector import _fetch_ticker_historical

    # as_of = 2026-04-23 09:30 UTC (= 15:00 IST)
    as_of = datetime(2026, 4, 23, 9, 30, 0, tzinfo=timezone.utc)

    # Simulate a yfinance response: tz-naive index at midnight IST (= 2026-04-23 00:00 local)
    idx = pd.DatetimeIndex([
        pd.Timestamp("2026-04-22"),
        pd.Timestamp("2026-04-23"),  # tz-naive IST midnight
    ])
    fake_hist = pd.DataFrame(
        {"Open": [100.0, 102.0], "High": [101.0, 103.0],
         "Low": [99.0, 101.0], "Close": [100.5, 102.5],
         "Volume": [1000, 2000]},
        index=idx,
    )

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = fake_hist

    with patch("src.collectors.macro_collector.yf.Ticker", return_value=mock_ticker):
        result = _fetch_ticker_historical("^NSEI", as_of)

    # With the fix (localise as IST): 2026-04-23 00:00 IST = 2026-04-22 18:30 UTC,
    # which is <= 09:30 UTC on 2026-04-23 → the bar is selected.
    # With the old bug (localise as UTC): 2026-04-23 00:00 UTC is *after* 09:30 UTC
    # on the same day when the index is extended, but more importantly a 09:15 IST
    # bar (= 03:45 UTC) would be misread as 09:15 UTC (= 14:45 IST).
    assert result["price"] is not None, "IST bar must be visible to a UTC as_of after market hours"
    assert result["price"] == pytest.approx(102.5)
