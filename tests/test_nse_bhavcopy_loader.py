"""Unit tests for the NSE bhavcopy historical loader (Task 3).

The DataFrame fetcher (``fetch_fo_bhavcopy``) is mocked so tests don't hit
the network. We verify:

  * Idempotency at the SQL level (the loader uses ``ON CONFLICT DO NOTHING``
    on the options_chain composite PK).
  * Symbol → instrument_id lookup; unknown symbols are skipped with a count.
  * Invalid rows (missing strike/expiry, non-CE/PE option_type) are skipped.
  * IV computation runs when underlying_ltp and settle_price are both present.
  * 404 (missing archive) returns zero counts cleanly.
  * Backfill loop sums per-date results and continues on per-day error.
"""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from src.quant.backtest.data_loaders import nse_bhavcopy


# ---------------------------------------------------------------------------
# Source-level invariants (no DB needed)
# ---------------------------------------------------------------------------

def test_loader_uses_on_conflict_do_nothing():
    src = inspect.getsource(nse_bhavcopy.load_one_date)
    assert "on_conflict_do_nothing" in src
    # All five PK columns must be in the conflict target.
    for col in ("instrument_id", "snapshot_at", "expiry_date",
                "strike_price", "option_type"):
        assert col in src


def test_compute_dte_years_after_expiry_zero():
    assert nse_bhavcopy._compute_dte_years(date(2026, 5, 10), date(2026, 5, 5)) == 0.0


def test_compute_dte_years_30_days():
    t = nse_bhavcopy._compute_dte_years(date(2026, 4, 27), date(2026, 5, 27))
    assert t == pytest.approx(30 / 365.0, abs=1e-9)


# ---------------------------------------------------------------------------
# load_one_date — mocked DataFrame fetcher + recording session
# ---------------------------------------------------------------------------

def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a normalised bhavcopy-shape DataFrame from row dicts."""
    return pd.DataFrame(rows)


class _RecordingSession:
    """Async session double — records every executed statement."""

    def __init__(self, sym_to_id: dict[str, str]):
        self.sym_to_id = sym_to_id
        self.executed: list = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        # First call inside load_one_date is the symbol→id lookup
        if len(self.executed) == 1:
            rows = [(inst_id, sym) for sym, inst_id in self.sym_to_id.items()]
            return SimpleNamespace(all=lambda: rows)
        return None


@pytest.mark.asyncio
async def test_load_one_date_inserts_rows_for_known_symbols(monkeypatch):
    df = _make_df([
        {
            "symbol": "RELIANCE",
            "option_type": "CE",
            "expiry_date": date(2026, 5, 28),
            "strike_price": 2500.0,
            "settle_price": 50.0,
            "close": 50.0,
            "underlying_price": 2500.0,
            "contracts": 100,
            "oi": 5000,
            "change_in_oi": 200,
        },
        {
            "symbol": "RELIANCE",
            "option_type": "PE",
            "expiry_date": date(2026, 5, 28),
            "strike_price": 2500.0,
            "settle_price": 45.0,
            "close": 45.0,
            "underlying_price": 2500.0,
            "contracts": 80,
            "oi": 4000,
            "change_in_oi": 100,
        },
    ])
    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", AsyncMock(return_value=df))

    session = _RecordingSession(sym_to_id={"RELIANCE": "abc-123"})

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(nse_bhavcopy, "session_scope", _scope)

    res = await nse_bhavcopy.load_one_date(date(2026, 4, 27))
    assert res["rows_in_csv"] == 2
    assert res["inserted"] == 2
    assert res["skipped_unknown_symbol"] == 0
    assert res["skipped_invalid"] == 0
    # 1 lookup + 2 inserts
    assert len(session.executed) == 3


@pytest.mark.asyncio
async def test_load_one_date_skips_unknown_symbol(monkeypatch):
    df = _make_df([
        {
            "symbol": "GHOSTCO",
            "option_type": "CE",
            "expiry_date": date(2026, 5, 28),
            "strike_price": 100.0,
            "settle_price": 5.0,
            "close": 5.0,
            "underlying_price": 100.0,
            "contracts": 1,
            "oi": 1,
            "change_in_oi": 0,
        },
    ])
    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", AsyncMock(return_value=df))
    session = _RecordingSession(sym_to_id={"RELIANCE": "abc-123"})

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(nse_bhavcopy, "session_scope", _scope)

    res = await nse_bhavcopy.load_one_date(date(2026, 4, 27))
    assert res["inserted"] == 0
    assert res["skipped_unknown_symbol"] == 1
    # Only the symbol lookup ran — no inserts.
    assert len(session.executed) == 1


@pytest.mark.asyncio
async def test_load_one_date_skips_invalid_option_type(monkeypatch):
    df = _make_df([
        {
            "symbol": "RELIANCE",
            "option_type": "FUT",  # not CE/PE
            "expiry_date": date(2026, 5, 28),
            "strike_price": 2500.0,
            "settle_price": 0.0,
            "close": 0.0,
            "underlying_price": 2500.0,
            "contracts": 1,
            "oi": 1,
            "change_in_oi": 0,
        },
    ])
    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", AsyncMock(return_value=df))
    session = _RecordingSession(sym_to_id={"RELIANCE": "abc-123"})

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(nse_bhavcopy, "session_scope", _scope)

    res = await nse_bhavcopy.load_one_date(date(2026, 4, 27))
    assert res["inserted"] == 0
    assert res["skipped_invalid"] == 1


@pytest.mark.asyncio
async def test_load_one_date_skips_when_strike_or_expiry_missing(monkeypatch):
    df = _make_df([
        {
            "symbol": "RELIANCE",
            "option_type": "CE",
            "expiry_date": None,    # missing
            "strike_price": 2500.0,
            "settle_price": 50.0,
            "close": 50.0,
            "underlying_price": 2500.0,
            "contracts": 1,
            "oi": 1,
            "change_in_oi": 0,
        },
        {
            "symbol": "RELIANCE",
            "option_type": "CE",
            "expiry_date": date(2026, 5, 28),
            "strike_price": None,    # missing
            "settle_price": 50.0,
            "close": 50.0,
            "underlying_price": 2500.0,
            "contracts": 1,
            "oi": 1,
            "change_in_oi": 0,
        },
    ])
    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", AsyncMock(return_value=df))
    session = _RecordingSession(sym_to_id={"RELIANCE": "abc-123"})

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(nse_bhavcopy, "session_scope", _scope)

    res = await nse_bhavcopy.load_one_date(date(2026, 4, 27))
    assert res["inserted"] == 0
    assert res["skipped_invalid"] == 2


@pytest.mark.asyncio
async def test_load_one_date_404_returns_zero_counts(monkeypatch):
    from src.dryrun.bhavcopy import BhavcopyMissingError

    async def _raise(_):
        raise BhavcopyMissingError("404 for date")

    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", _raise)

    res = await nse_bhavcopy.load_one_date(date(2026, 5, 2))
    assert res == {
        "rows_in_csv": 0,
        "inserted": 0,
        "skipped_unknown_symbol": 0,
        "skipped_invalid": 0,
    }


@pytest.mark.asyncio
async def test_load_one_date_iv_computed_when_inputs_present(monkeypatch):
    iv_calls = []

    def _capture_iv(market_price, S, K, T, r, opt):
        iv_calls.append((market_price, S, K, T, r, opt))
        return 0.20

    monkeypatch.setattr(nse_bhavcopy, "compute_iv", _capture_iv)
    df = _make_df([
        {
            "symbol": "RELIANCE",
            "option_type": "CE",
            "expiry_date": date(2026, 5, 28),
            "strike_price": 2500.0,
            "settle_price": 50.0,
            "close": 50.0,
            "underlying_price": 2500.0,
            "contracts": 1,
            "oi": 1,
            "change_in_oi": 0,
        },
    ])
    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", AsyncMock(return_value=df))
    session = _RecordingSession(sym_to_id={"RELIANCE": "abc-123"})

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(nse_bhavcopy, "session_scope", _scope)

    await nse_bhavcopy.load_one_date(date(2026, 4, 27))
    assert len(iv_calls) == 1
    market_price, S, K, T, r, opt = iv_calls[0]
    assert market_price == 50.0
    assert S == 2500.0
    assert K == 2500.0
    assert opt == "CE"


@pytest.mark.asyncio
async def test_load_one_date_iv_skipped_when_underlying_missing(monkeypatch):
    """When underlying_price is missing, IV is left None — no compute_iv call."""
    iv_calls = []
    monkeypatch.setattr(
        nse_bhavcopy, "compute_iv", lambda *a, **k: iv_calls.append(a) or 0.20
    )
    df = _make_df([
        {
            "symbol": "RELIANCE",
            "option_type": "CE",
            "expiry_date": date(2026, 5, 28),
            "strike_price": 2500.0,
            "settle_price": 50.0,
            "close": 50.0,
            "underlying_price": None,
            "contracts": 1,
            "oi": 1,
            "change_in_oi": 0,
        },
    ])
    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", AsyncMock(return_value=df))
    session = _RecordingSession(sym_to_id={"RELIANCE": "abc-123"})

    @asynccontextmanager
    async def _scope():
        yield session

    monkeypatch.setattr(nse_bhavcopy, "session_scope", _scope)

    res = await nse_bhavcopy.load_one_date(date(2026, 4, 27))
    assert res["inserted"] == 1
    assert iv_calls == []


# ---------------------------------------------------------------------------
# backfill loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_aggregates_per_date_results(monkeypatch):
    per_date_results = [
        {
            "rows_in_csv": 100,
            "inserted": 95,
            "skipped_unknown_symbol": 3,
            "skipped_invalid": 2,
        },
        {
            "rows_in_csv": 110,
            "inserted": 100,
            "skipped_unknown_symbol": 5,
            "skipped_invalid": 5,
        },
    ]
    load_mock = AsyncMock(side_effect=per_date_results)
    monkeypatch.setattr(nse_bhavcopy, "load_one_date", load_mock)

    res = await nse_bhavcopy.backfill(date(2026, 4, 27), date(2026, 4, 28))
    assert res["days"] == 2
    assert res["rows_in_csv"] == 210
    assert res["inserted"] == 195
    assert res["skipped_unknown_symbol"] == 8
    assert res["skipped_invalid"] == 7
    assert res["failed_days"] == 0


@pytest.mark.asyncio
async def test_backfill_continues_on_per_day_error(monkeypatch):
    # First date errors; second succeeds.
    side_effects = [
        RuntimeError("transient NSE blip"),
        {"rows_in_csv": 50, "inserted": 50, "skipped_unknown_symbol": 0, "skipped_invalid": 0},
    ]
    load_mock = AsyncMock(side_effect=side_effects)
    monkeypatch.setattr(nse_bhavcopy, "load_one_date", load_mock)

    res = await nse_bhavcopy.backfill(date(2026, 4, 27), date(2026, 4, 28))
    assert res["failed_days"] == 1
    assert res["inserted"] == 50  # only the successful day contributed


@pytest.mark.asyncio
async def test_backfill_empty_range_no_calls(monkeypatch):
    load_mock = AsyncMock()
    monkeypatch.setattr(nse_bhavcopy, "load_one_date", load_mock)
    # Sat + Sun → 0 trading days
    res = await nse_bhavcopy.backfill(date(2026, 5, 2), date(2026, 5, 3))
    assert res["days"] == 0
    assert load_mock.await_count == 0
