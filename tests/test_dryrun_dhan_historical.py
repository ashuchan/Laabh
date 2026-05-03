"""Tests for Task 5 — DhanHistoricalSource."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


def _make_bhavcopy_df():
    """Minimal bhavcopy DataFrame with two liquid NIFTY contracts."""
    return pd.DataFrame([
        {
            "symbol": "NIFTY",
            "expiry_date": date(2026, 4, 24),
            "strike_price": 22000.0,
            "option_type": "CE",
            "oi": 5000,
            "contracts": 500,
            "close": 155.0,
        },
        {
            "symbol": "NIFTY",
            "expiry_date": date(2026, 4, 24),
            "strike_price": 22000.0,
            "option_type": "PE",
            "oi": 4000,
            "contracts": 400,
            "close": 132.0,
        },
    ])


def _make_instrument_master():
    return {
        "NIFTY|20260424|22000.00|CE": "SEC001",
        "NIFTY|20260424|22000.00|PE": "SEC002",
    }


def _make_candles(as_of: datetime):
    """Two candles: one before as_of, one after."""
    ts_before = as_of.timestamp() - 300  # 5 min before
    ts_after = as_of.timestamp() + 300   # 5 min after
    return [
        {"timestamp": ts_before, "close": 158.0, "oi": 5100},
        {"timestamp": ts_after, "close": 162.0, "oi": 5200},
    ]


@pytest.mark.asyncio
async def test_fetch_returns_chain_snapshot(tmp_path):
    """Happy path: fetch() returns a ChainSnapshot with strikes from bhavcopy."""
    from src.fno.sources.dhan_historical import DhanHistoricalSource

    D = date(2026, 4, 23)
    as_of = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)

    source = DhanHistoricalSource.__new__(DhanHistoricalSource)
    source._replay_date = D
    source._bhavcopy_df = _make_bhavcopy_df()
    source._instrument_master = _make_instrument_master()
    source._cache_dir = tmp_path
    import asyncio
    source._semaphore = asyncio.Semaphore(10)
    source._client = None

    candles = _make_candles(as_of)

    async def fake_fetch_candles(sec_id, as_of_dt):
        return candles

    source._fetch_candles = fake_fetch_candles

    with patch("src.dryrun.bhavcopy.fetch_cm_bhavcopy", new=AsyncMock(return_value=pd.DataFrame())):
        snapshot = await source.fetch("NIFTY", date(2026, 4, 24), as_of=as_of)

    assert snapshot.symbol == "NIFTY"
    assert snapshot.expiry_date == date(2026, 4, 24)
    assert len(snapshot.strikes) == 2  # CE and PE

    opt_types = {r.option_type for r in snapshot.strikes}
    assert opt_types == {"CE", "PE"}


@pytest.mark.asyncio
async def test_fetch_falls_back_to_bhavcopy_close_when_no_security_id(tmp_path):
    """When security_id lookup fails, ltp comes from bhavcopy close price."""
    from src.fno.sources.dhan_historical import DhanHistoricalSource
    from decimal import Decimal

    D = date(2026, 4, 23)
    as_of = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)

    source = DhanHistoricalSource.__new__(DhanHistoricalSource)
    source._replay_date = D
    source._bhavcopy_df = _make_bhavcopy_df()
    source._instrument_master = {}  # empty — no security IDs
    source._cache_dir = tmp_path
    import asyncio
    source._semaphore = asyncio.Semaphore(10)
    source._client = None

    with patch("src.dryrun.bhavcopy.fetch_cm_bhavcopy", new=AsyncMock(return_value=pd.DataFrame())):
        snapshot = await source.fetch("NIFTY", date(2026, 4, 24), as_of=as_of)

    assert len(snapshot.strikes) == 2
    ce_row = next(r for r in snapshot.strikes if r.option_type == "CE")
    pe_row = next(r for r in snapshot.strikes if r.option_type == "PE")
    assert ce_row.ltp == Decimal("155.0")
    assert pe_row.ltp == Decimal("132.0")


@pytest.mark.asyncio
async def test_candle_fetch_cached(tmp_path):
    """Second call to _fetch_candles uses disk cache, not HTTP."""
    from src.fno.sources.dhan_historical import DhanHistoricalSource

    D = date(2026, 4, 23)
    as_of = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)

    source = DhanHistoricalSource.__new__(DhanHistoricalSource)
    source._replay_date = D
    source._cache_dir = tmp_path
    import asyncio
    source._semaphore = asyncio.Semaphore(10)
    source._client = None

    candles = _make_candles(as_of)
    cache_file = tmp_path / "SEC999_5.json"
    with cache_file.open("w") as f:
        json.dump(candles, f)

    with patch.object(source, "_headers", return_value={}):
        result = await source._fetch_candles("SEC999", as_of)

    # Should return the cached candles without making HTTP calls
    assert len(result) == 2


def test_pick_candle_selects_at_or_before():
    """_pick_candle returns the last candle at-or-before as_of."""
    from src.fno.sources.dhan_historical import DhanHistoricalSource

    as_of = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    candles = [
        {"timestamp": as_of.timestamp() - 600, "close": 100.0, "oi": 1000},
        {"timestamp": as_of.timestamp() - 300, "close": 105.0, "oi": 1100},  # best
        {"timestamp": as_of.timestamp() + 300, "close": 110.0, "oi": 1200},  # future
    ]
    result = DhanHistoricalSource._pick_candle(candles, as_of)
    assert result is not None
    assert result["close"] == 105.0


def test_pick_candle_returns_none_when_all_future():
    """_pick_candle returns None when all candles are in the future."""
    from src.fno.sources.dhan_historical import DhanHistoricalSource

    as_of = datetime(2026, 4, 23, 9, 0, 0, tzinfo=timezone.utc)
    candles = [
        {"timestamp": as_of.timestamp() + 300, "close": 100.0, "oi": 1000},
        {"timestamp": as_of.timestamp() + 600, "close": 105.0, "oi": 1100},
    ]
    result = DhanHistoricalSource._pick_candle(candles, as_of)
    assert result is None


def test_pick_candle_empty():
    """_pick_candle returns None for empty candle list."""
    from src.fno.sources.dhan_historical import DhanHistoricalSource

    as_of = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    result = DhanHistoricalSource._pick_candle([], as_of)
    assert result is None
