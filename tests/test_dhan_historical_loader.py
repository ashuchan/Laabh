"""Unit tests for the Dhan historical OHLC loader (Task 2).

HTTP and DB are mocked. We verify:

  * OHLC arithmetic invariants reject malformed candles.
  * Idempotency: ``load_one_instrument_one_date`` uses ON CONFLICT DO NOTHING
    on (instrument_id, timestamp).
  * Resume: ``_resume_point`` reads ``MAX(timestamp)`` from price_intraday.
  * Backfill enumerates ``instruments × trading_days`` correctly and respects
    the resume point (no re-fetch of already-loaded dates).
  * Rate limiter respects requests-per-minute budget under burst.
  * Per-day error doesn't abort the whole backfill.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytz

from src.quant.backtest.data_loaders import dhan_historical


_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# OHLC validation
# ---------------------------------------------------------------------------

def test_validate_ohlc_well_formed():
    assert dhan_historical._validate_ohlc(o=100, h=105, l=98, c=103, v=1000)


def test_validate_ohlc_low_above_open_rejected():
    assert not dhan_historical._validate_ohlc(o=100, h=105, l=101, c=103, v=1000)


def test_validate_ohlc_high_below_close_rejected():
    assert not dhan_historical._validate_ohlc(o=100, h=102, l=98, c=103, v=1000)


def test_validate_ohlc_negative_volume_rejected():
    assert not dhan_historical._validate_ohlc(o=100, h=105, l=98, c=103, v=-1)


# ---------------------------------------------------------------------------
# Source-level idempotency (insert path uses on_conflict_do_nothing)
# ---------------------------------------------------------------------------

def test_load_one_instrument_uses_on_conflict_do_nothing():
    src = inspect.getsource(dhan_historical.load_one_instrument_one_date)
    assert "on_conflict_do_nothing" in src
    assert "instrument_id" in src
    assert "timestamp" in src


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def test_parse_timestamp_epoch_seconds_round_trips_to_same_instant():
    """Epoch is unambiguous: a UTC epoch must round-trip to the same instant.

    The previous implementation localized the epoch as IST and then converted
    to UTC, introducing a 5h30m shift. This test asserts the value, not just
    the timezone, so that bug can't recur.
    """
    ist_dt = _IST.localize(datetime(2026, 4, 27, 9, 15, 0))
    epoch = ist_dt.timestamp()
    parsed = dhan_historical._parse_dhan_timestamp(epoch)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # 09:15 IST == 03:45 UTC same date
    assert parsed.year == 2026 and parsed.month == 4 and parsed.day == 27
    assert parsed.hour == 3 and parsed.minute == 45


def test_parse_timestamp_iso_string_returns_utc():
    parsed = dhan_historical._parse_dhan_timestamp("2026-04-27T09:15:00+05:30")
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.hour == 3 and parsed.minute == 45


def test_parse_timestamp_none_returns_none():
    assert dhan_historical._parse_dhan_timestamp(None) is None


def test_parse_timestamp_garbage_returns_none():
    assert dhan_historical._parse_dhan_timestamp("not a timestamp") is None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_under_budget_does_not_block():
    """First N requests under the per-minute cap should not block."""
    limiter = dhan_historical._RateLimiter(max_per_min=10)
    t0 = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5  # 5 acquires under cap should be near-instant


@pytest.mark.asyncio
async def test_rate_limiter_blocks_when_budget_exhausted(monkeypatch):
    """When the budget is full, the next acquire waits until oldest stamp ages out.

    We patch ``asyncio.sleep`` to verify the waiting branch is exercised
    without burning real time.
    """
    sleep_calls: list[float] = []

    real_sleep = asyncio.sleep

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)
        # Move "monotonic" forward by faking the timestamps inside the
        # limiter so the oldest stamp ages out.
        # Easiest approach: rewind the recorded stamps by `seconds`.
        for i in range(len(limiter._stamps)):
            limiter._stamps[i] -= seconds
        await real_sleep(0)

    limiter = dhan_historical._RateLimiter(max_per_min=2)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    # Fill the bucket
    await limiter.acquire()
    await limiter.acquire()
    # Third call must block (record at least one sleep)
    await limiter.acquire()
    assert any(s > 0 for s in sleep_calls), "limiter did not block when bucket full"


# ---------------------------------------------------------------------------
# load_one_instrument_one_date — mocked HTTP + DB
# ---------------------------------------------------------------------------

class _RecordingSession:
    def __init__(self):
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return None


@pytest.mark.asyncio
async def test_load_one_inserts_valid_candles(monkeypatch):
    candles = [
        {
            "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5,
            "volume": 100, "timestamp": _IST.localize(
                datetime(2026, 4, 27, 9, 15)
            ).timestamp(),
        },
        {
            "open": 100.5, "high": 101.5, "low": 100.0, "close": 101.0,
            "volume": 200, "timestamp": _IST.localize(
                datetime(2026, 4, 27, 9, 16)
            ).timestamp(),
        },
    ]
    monkeypatch.setattr(
        dhan_historical, "_fetch_one_day", AsyncMock(return_value=candles)
    )
    session = _RecordingSession()

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(dhan_historical, "session_scope", _scope)

    res = await dhan_historical.load_one_instrument_one_date(
        instrument_id=uuid.uuid4(),
        symbol="RELIANCE",
        security_id="2885",
        target_date=date(2026, 4, 27),
        client=None,  # not used because _fetch_one_day is mocked
    )
    assert res == {"fetched": 2, "inserted": 2, "invalid": 0}
    assert len(session.executed) == 2


@pytest.mark.asyncio
async def test_load_one_skips_invalid_ohlc(monkeypatch):
    candles = [
        # Valid
        {"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1,
         "timestamp": _IST.localize(datetime(2026, 4, 27, 9, 15)).timestamp()},
        # high < low — invalid
        {"open": 100, "high": 99, "low": 102, "close": 100, "volume": 1,
         "timestamp": _IST.localize(datetime(2026, 4, 27, 9, 16)).timestamp()},
        # Negative volume — invalid
        {"open": 100, "high": 101, "low": 99, "close": 100, "volume": -1,
         "timestamp": _IST.localize(datetime(2026, 4, 27, 9, 17)).timestamp()},
    ]
    monkeypatch.setattr(
        dhan_historical, "_fetch_one_day", AsyncMock(return_value=candles)
    )
    session = _RecordingSession()

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(dhan_historical, "session_scope", _scope)

    res = await dhan_historical.load_one_instrument_one_date(
        instrument_id=uuid.uuid4(),
        symbol="RELIANCE",
        security_id="2885",
        target_date=date(2026, 4, 27),
        client=None,
    )
    assert res == {"fetched": 3, "inserted": 1, "invalid": 2}


@pytest.mark.asyncio
async def test_load_one_empty_candle_list_returns_zeros(monkeypatch):
    monkeypatch.setattr(
        dhan_historical, "_fetch_one_day", AsyncMock(return_value=[])
    )
    res = await dhan_historical.load_one_instrument_one_date(
        instrument_id=uuid.uuid4(),
        symbol="X",
        security_id="0",
        target_date=date(2026, 4, 27),
        client=None,
    )
    assert res == {"fetched": 0, "inserted": 0, "invalid": 0}


# ---------------------------------------------------------------------------
# Resume point
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_point_returns_none_when_table_empty():
    inst_id = uuid.uuid4()

    class _S:
        async def execute(self, _stmt):
            # Mirror SQLAlchemy: .first() returns row tuple; here MAX(...) is None.
            return SimpleNamespace(first=lambda: (None,))

    res = await dhan_historical._resume_point(_S(), inst_id)
    assert res is None


@pytest.mark.asyncio
async def test_resume_point_returns_ist_date():
    inst_id = uuid.uuid4()
    last_utc = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)

    class _S:
        async def execute(self, _stmt):
            return SimpleNamespace(first=lambda: (last_utc,))

    res = await dhan_historical._resume_point(_S(), inst_id)
    # 10:00 UTC → 15:30 IST same date
    assert res == date(2026, 4, 27)


# ---------------------------------------------------------------------------
# backfill orchestration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_skips_dates_at_or_before_resume(monkeypatch):
    inst_id = uuid.uuid4()

    # Resume point says "loaded through 2026-04-27"
    monkeypatch.setattr(
        dhan_historical, "_resume_point", AsyncMock(return_value=date(2026, 4, 27))
    )

    @asynccontextmanager
    async def _scope():
        yield None

    monkeypatch.setattr(dhan_historical, "session_scope", _scope)

    load_mock = AsyncMock(
        return_value={"fetched": 100, "inserted": 100, "invalid": 0}
    )
    monkeypatch.setattr(
        dhan_historical, "load_one_instrument_one_date", load_mock
    )

    res = await dhan_historical.backfill(
        instruments=[(inst_id, "RELIANCE", "2885")],
        start_date=date(2026, 4, 27),
        end_date=date(2026, 4, 29),  # Mon-Wed: 3 trading days
        rate_limit_per_min=100,  # don't throttle in tests
    )
    # 4/27 is at-or-before resume → skipped
    # 4/28 and 4/29 → 2 calls
    assert res["calls"] == 2
    assert res["skipped_resume"] == 1
    assert load_mock.await_count == 2


@pytest.mark.asyncio
async def test_backfill_continues_on_per_call_error(monkeypatch):
    inst_id = uuid.uuid4()
    monkeypatch.setattr(
        dhan_historical, "_resume_point", AsyncMock(return_value=None)
    )

    @asynccontextmanager
    async def _scope():
        yield None

    monkeypatch.setattr(dhan_historical, "session_scope", _scope)

    side = [
        {"fetched": 50, "inserted": 50, "invalid": 0},
        RuntimeError("transient"),
        {"fetched": 30, "inserted": 30, "invalid": 0},
    ]
    monkeypatch.setattr(
        dhan_historical,
        "load_one_instrument_one_date",
        AsyncMock(side_effect=side),
    )

    res = await dhan_historical.backfill(
        instruments=[(inst_id, "X", "0")],
        start_date=date(2026, 4, 27),
        end_date=date(2026, 4, 29),
        rate_limit_per_min=100,
    )
    assert res["calls"] == 3
    assert res["failed_calls"] == 1
    assert res["fetched"] == 80
    assert res["inserted"] == 80


@pytest.mark.asyncio
async def test_backfill_no_instruments_no_calls(monkeypatch):
    res = await dhan_historical.backfill(
        instruments=[],
        start_date=date(2026, 4, 27),
        end_date=date(2026, 4, 30),
        rate_limit_per_min=100,
    )
    assert res["instruments"] == 0
    assert res["calls"] == 0


@pytest.mark.asyncio
async def test_segment_for_index_vs_equity():
    assert dhan_historical._segment_for("NIFTY") == dhan_historical._SEG_INDEX
    assert dhan_historical._segment_for("BANKNIFTY") == dhan_historical._SEG_INDEX
    assert dhan_historical._segment_for("RELIANCE") == dhan_historical._SEG_EQUITY
    assert dhan_historical._segment_for("hdfcbank") == dhan_historical._SEG_EQUITY


# ---------------------------------------------------------------------------
# Tenacity retry on transient errors (M3 fix)
# ---------------------------------------------------------------------------

def test_fetch_one_day_decorated_with_tenacity():
    """The function must be wrapped with tenacity so retries fire on
    transient errors. Inspect the wrapped function's attributes (tenacity
    sets ``retry`` on the wrapper)."""
    fn = dhan_historical._fetch_one_day
    assert hasattr(fn, "retry"), "expected tenacity wrapper on _fetch_one_day"


def test_dhan_transient_subclass_of_runtime_error():
    """``_DhanTransient`` is the retry-eligible subtype; staying a subclass
    of RuntimeError means existing ``except RuntimeError`` paths still catch it.
    """
    assert issubclass(dhan_historical._DhanTransient, RuntimeError)


@pytest.mark.asyncio
async def test_5xx_raises_transient_for_retry(monkeypatch):
    """500/503/etc. responses must surface as ``_DhanTransient`` so
    tenacity retries (not raise as a permanent ``RuntimeError``).

    We exercise this by stubbing the underlying client and asserting that
    after exhausting retries the final raised type is ``_DhanTransient``.

    Test speed: tenacity's exponential backoff (2s → 16s) would make this
    take ~30s wall-clock. We swap the wait policy for ``wait_none()`` so
    the test runs in milliseconds while still exercising the retry budget.
    """
    import tenacity
    monkeypatch.setattr(
        dhan_historical._fetch_one_day.retry, "wait", tenacity.wait_none()
    )

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "boom"

    class _Client:
        def __init__(self):
            self.calls = 0

        async def post(self, *_a, **_k):
            self.calls += 1
            return _Resp(503)

    monkeypatch.setattr(
        "src.quant.backtest.data_loaders.dhan_historical.get_dhan_headers",
        AsyncMock(return_value={}),
    )

    client = _Client()
    with pytest.raises(dhan_historical._DhanTransient):
        await dhan_historical._fetch_one_day(
            client=client,
            security_id="2885",
            symbol="RELIANCE",
            target_date=date(2026, 4, 27),
        )
    # Tenacity stop_after_attempt(5) → 5 calls total
    assert client.calls == 5


@pytest.mark.asyncio
async def test_4xx_other_than_401_fails_fast(monkeypatch):
    """400/404 must NOT retry (permanent client errors)."""
    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "bad request"

    class _Client:
        def __init__(self):
            self.calls = 0

        async def post(self, *_a, **_k):
            self.calls += 1
            return _Resp(400)

    monkeypatch.setattr(
        "src.quant.backtest.data_loaders.dhan_historical.get_dhan_headers",
        AsyncMock(return_value={}),
    )
    client = _Client()
    with pytest.raises(RuntimeError, match="400"):
        await dhan_historical._fetch_one_day(
            client=client,
            security_id="2885",
            symbol="RELIANCE",
            target_date=date(2026, 4, 27),
        )
    # No retry — single call
    assert client.calls == 1
