"""Unit tests for Task 4 auxiliary loaders.

The DB-touching pieces are mocked: each loader's responsibility is to
enumerate a date range and delegate per-day work to the upstream live-mode
fetcher (which is already tested elsewhere). We verify:

  * The date enumeration is correct (skips weekends + holidays).
  * The upstream fetcher is called once per trading day.
  * Failures on individual days don't abort the whole backfill.
  * RBI repo CSV parser handles header rows, ISO dates, junk lines.
  * RBI loader is idempotent (parser drops invalid rows; upsert is
    on-conflict-do-update).
"""
from __future__ import annotations

import textwrap
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.quant.backtest.data_loaders import (
    nse_ban_list_history,
    nse_vix_history,
    rbi_repo_history,
)


# ---------------------------------------------------------------------------
# nse_ban_list_history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ban_list_backfill_calls_fetch_today_per_trading_day(monkeypatch):
    """fetch_today is called once per business day, skipping weekends."""
    fetch_mock = AsyncMock(return_value=3)
    monkeypatch.setattr(nse_ban_list_history, "fetch_today", fetch_mock)

    # Fri 2026-05-01 → Mon 2026-05-04 (skips Sat 5/2 and Sun 5/3)
    result = await nse_ban_list_history.backfill(
        date(2026, 5, 1), date(2026, 5, 4)
    )

    assert result["days"] == 2
    assert result["inserted"] == 6  # 3 per day × 2 days
    assert fetch_mock.await_count == 2
    called_dates = [c.kwargs["ban_date"] for c in fetch_mock.await_args_list]
    assert called_dates == [date(2026, 5, 1), date(2026, 5, 4)]


@pytest.mark.asyncio
async def test_ban_list_backfill_skips_holidays(monkeypatch):
    fetch_mock = AsyncMock(return_value=0)
    monkeypatch.setattr(nse_ban_list_history, "fetch_today", fetch_mock)
    holiday = date(2026, 4, 29)  # Wed

    await nse_ban_list_history.backfill(
        date(2026, 4, 27), date(2026, 5, 1), holidays={holiday}
    )

    called_dates = {c.kwargs["ban_date"] for c in fetch_mock.await_args_list}
    assert holiday not in called_dates
    assert fetch_mock.await_count == 4  # 5 weekdays minus 1 holiday


@pytest.mark.asyncio
async def test_ban_list_backfill_continues_on_per_day_error(monkeypatch):
    """Single-day exception must not abort the whole loop."""
    side_effects = [3, RuntimeError("network blip"), 5]
    fetch_mock = AsyncMock(side_effect=side_effects)
    monkeypatch.setattr(nse_ban_list_history, "fetch_today", fetch_mock)

    result = await nse_ban_list_history.backfill(
        date(2026, 4, 27), date(2026, 4, 29)  # Mon-Wed (3 days)
    )

    assert result["days"] == 3
    assert result["inserted"] == 8  # 3 + 5 (middle day errored)
    assert result["skipped_404"] == 1


@pytest.mark.asyncio
async def test_ban_list_backfill_empty_range_no_calls(monkeypatch):
    fetch_mock = AsyncMock(return_value=0)
    monkeypatch.setattr(nse_ban_list_history, "fetch_today", fetch_mock)
    result = await nse_ban_list_history.backfill(
        date(2026, 5, 2), date(2026, 5, 3)  # Sat + Sun
    )
    assert result == {"days": 0, "inserted": 0, "skipped_404": 0}
    assert fetch_mock.await_count == 0


# ---------------------------------------------------------------------------
# nse_vix_history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vix_backfill_calls_run_once_per_day_at_close(monkeypatch):
    run_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(nse_vix_history, "run_once", run_mock)

    await nse_vix_history.backfill(date(2026, 4, 27), date(2026, 4, 28))

    assert run_mock.await_count == 2
    # Each call is at 15:30 IST on the trading date
    for call in run_mock.await_args_list:
        as_of = call.kwargs["as_of"]
        assert as_of.tzinfo is not None
        assert as_of.time().hour == 15 and as_of.time().minute == 30


@pytest.mark.asyncio
async def test_vix_backfill_continues_on_failure(monkeypatch):
    side_effects = [None, RuntimeError("yfinance hiccup"), None]
    run_mock = AsyncMock(side_effect=side_effects)
    monkeypatch.setattr(nse_vix_history, "run_once", run_mock)

    result = await nse_vix_history.backfill(
        date(2026, 4, 27), date(2026, 4, 29)
    )
    assert result == {"days": 3, "fetched": 2, "failed": 1}


@pytest.mark.asyncio
async def test_vix_backfill_no_calls_for_empty_range(monkeypatch):
    run_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(nse_vix_history, "run_once", run_mock)
    res = await nse_vix_history.backfill(date(2026, 5, 3), date(2026, 5, 2))
    assert res == {"days": 0, "fetched": 0, "failed": 0}
    assert run_mock.await_count == 0


# ---------------------------------------------------------------------------
# rbi_repo_history — CSV parser
# ---------------------------------------------------------------------------

def test_rbi_parse_csv_with_header():
    text = textwrap.dedent(
        """\
        date,repo_rate_pct
        2020-03-27,4.40
        2020-05-22,4.00
        2022-05-04,4.40
        """
    )
    out = rbi_repo_history._parse_csv(text)
    assert out == [
        (date(2020, 3, 27), Decimal("4.40")),
        (date(2020, 5, 22), Decimal("4.00")),
        (date(2022, 5, 4), Decimal("4.40")),
    ]


def test_rbi_parse_csv_without_header():
    text = "2020-03-27,4.40\n2020-05-22,4.00\n"
    out = rbi_repo_history._parse_csv(text)
    assert len(out) == 2


def test_rbi_parse_csv_skips_junk_rows():
    text = textwrap.dedent(
        """\
        date,repo_rate_pct
        not-a-date,4.40
        2020-03-27,not-a-number
        2020-03-27,4.40
        ,
        """
    )
    out = rbi_repo_history._parse_csv(text)
    assert out == [(date(2020, 3, 27), Decimal("4.40"))]


def test_rbi_parse_csv_empty_returns_empty():
    assert rbi_repo_history._parse_csv("") == []


# ---------------------------------------------------------------------------
# rbi_repo_history — load_from_csv (mocked DB)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rbi_load_from_csv_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await rbi_repo_history.load_from_csv(tmp_path / "nope.csv")


@pytest.mark.asyncio
async def test_rbi_load_from_csv_parses_and_upserts(tmp_path, monkeypatch):
    csv_path = tmp_path / "repo.csv"
    csv_path.write_text(
        "date,repo_rate_pct\n2020-03-27,4.40\n2022-05-04,4.40\n",
        encoding="utf-8",
    )

    # Patch session_scope to a no-op that records executed statements
    from contextlib import asynccontextmanager

    executed_statements = []

    class _RecordingSession:
        async def execute(self, stmt):
            executed_statements.append(stmt)

    @asynccontextmanager
    async def _scope():
        yield _RecordingSession()

    monkeypatch.setattr(
        "src.quant.backtest.data_loaders.rbi_repo_history.session_scope",
        _scope,
    )

    res = await rbi_repo_history.load_from_csv(csv_path, source="rbi.org.in")
    assert res == {"parsed": 2, "upserted": 2}
    assert len(executed_statements) == 2


@pytest.mark.asyncio
async def test_rbi_load_from_csv_empty_csv_no_db_calls(tmp_path, monkeypatch):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("date,repo_rate_pct\n", encoding="utf-8")

    from contextlib import asynccontextmanager
    called = []

    @asynccontextmanager
    async def _scope():
        called.append(1)
        yield None

    monkeypatch.setattr(
        "src.quant.backtest.data_loaders.rbi_repo_history.session_scope",
        _scope,
    )
    res = await rbi_repo_history.load_from_csv(csv_path)
    assert res == {"parsed": 0, "upserted": 0}
    assert called == []  # no session opened when nothing to write


# ---------------------------------------------------------------------------
# Idempotency (the upsert clauses themselves)
# ---------------------------------------------------------------------------

def test_rbi_upsert_uses_on_conflict_do_update():
    """The upsert statement compiles to ``ON CONFLICT ... DO UPDATE``.

    Directly inspecting the source ensures the loader uses the upsert idiom
    (re-running the same date overwrites instead of erroring).
    """
    import inspect
    from src.quant.backtest.data_loaders import rbi_repo_history as rrh
    src = inspect.getsource(rrh.load_from_csv)
    assert "on_conflict_do_update" in src
    assert "index_elements=[\"date\"]" in src
