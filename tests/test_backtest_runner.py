"""Unit tests for ``BacktestRunner`` and the CLI harness.

The orchestrator's ``run_loop`` is mocked — the runner's job is purely
date-range orchestration, not the per-day replay logic (which is tested
elsewhere). We verify:

  * Date enumeration respects holidays + weekends.
  * One ``backtest_runs`` row is created per trading day.
  * Per-day errors are captured into ``SingleDayResult.failed`` without
    aborting the loop.
  * Aggregate P&L compounds geometrically across days.
  * The CLI argparse layer rejects bad inputs and accepts good ones.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.quant.backtest.runner import (
    BacktestRangeResult,
    BacktestRunner,
    SingleDayResult,
)


# ---------------------------------------------------------------------------
# BacktestRangeResult — pure data
# ---------------------------------------------------------------------------

def test_range_result_n_days_n_failed_total_trades():
    r = BacktestRangeResult(
        portfolio_id=uuid.uuid4(),
        start_date=date(2026, 4, 27),
        end_date=date(2026, 4, 29),
        days=[
            SingleDayResult(date(2026, 4, 27), uuid.uuid4(), 1.0e6, 1.01e6, 0.01, 5),
            SingleDayResult(date(2026, 4, 28), uuid.uuid4(), 1.0e6, None, None, None, failed=True, error="x"),
            SingleDayResult(date(2026, 4, 29), uuid.uuid4(), 1.0e6, 1.02e6, 0.02, 7),
        ],
    )
    assert r.n_days == 3
    assert r.n_failed == 1
    assert r.total_trade_count == 12  # 5 + 7 (failed contributes None → 0)


def test_cumulative_pnl_pct_compounds_geometrically():
    r = BacktestRangeResult(
        portfolio_id=uuid.uuid4(),
        start_date=date(2026, 4, 27),
        end_date=date(2026, 4, 29),
        days=[
            SingleDayResult(date(2026, 4, 27), uuid.uuid4(), 1e6, 1.01e6, 0.01, 1),
            SingleDayResult(date(2026, 4, 28), uuid.uuid4(), 1e6, 1.02e6, 0.02, 1),
        ],
    )
    # (1 + 0.01)(1 + 0.02) - 1 = 0.0302
    assert r.cumulative_pnl_pct == pytest.approx(0.0302, abs=1e-6)


def test_cumulative_pnl_pct_skips_none_days():
    r = BacktestRangeResult(
        portfolio_id=uuid.uuid4(),
        start_date=date(2026, 4, 27),
        end_date=date(2026, 4, 29),
        days=[
            SingleDayResult(date(2026, 4, 27), uuid.uuid4(), 1e6, None, None, None, failed=True),
            SingleDayResult(date(2026, 4, 28), uuid.uuid4(), 1e6, 1.05e6, 0.05, 1),
        ],
    )
    assert r.cumulative_pnl_pct == pytest.approx(0.05, abs=1e-6)


def test_cumulative_pnl_pct_zero_days():
    r = BacktestRangeResult(
        portfolio_id=uuid.uuid4(),
        start_date=date(2026, 4, 27),
        end_date=date(2026, 4, 29),
        days=[],
    )
    assert r.cumulative_pnl_pct == 0.0


# ---------------------------------------------------------------------------
# Runner construction + basic wiring
# ---------------------------------------------------------------------------

def test_runner_construct_uses_settings_defaults():
    runner = BacktestRunner(portfolio_id=uuid.uuid4())
    assert runner._seed == 42
    assert runner._smile_method in ("flat", "linear", "sabr")


def test_runner_construct_overrides():
    pid = uuid.uuid4()
    runner = BacktestRunner(
        portfolio_id=pid,
        seed=7,
        holidays=[date(2026, 4, 29)],
        risk_free_rate=0.07,
        smile_method="flat",
        chain_source="dhan_historical",
        underlying_source="yfinance",
    )
    assert runner._portfolio_id == pid
    assert runner._seed == 7
    assert runner._smile_method == "flat"
    assert runner._risk_free_rate == 0.07
    assert runner._chain_source == "dhan_historical"
    assert runner._underlying_source == "yfinance"
    assert runner._holidays == frozenset({date(2026, 4, 29)})


def test_runner_build_context_sets_backtest_mode():
    runner = BacktestRunner(portfolio_id=uuid.uuid4())
    ctx = runner._build_context(
        trading_date=date(2026, 4, 27),
        backtest_run_id=uuid.uuid4(),
    )
    assert ctx.mode == "backtest"
    # Clock satisfies the Protocol
    from src.quant.clock import Clock
    assert isinstance(ctx.clock, Clock)
    # Recorder is the backtest variant
    from src.quant.recorder import BacktestTradeRecorder
    assert isinstance(ctx.recorder, BacktestTradeRecorder)
    # Universe selector is the top-gainers variant
    from src.quant.backtest.universe_top_gainers import TopGainersUniverseSelector
    assert isinstance(ctx.universe_selector, TopGainersUniverseSelector)


def test_runner_lookahead_guard_enabled_by_default():
    """Default-constructed runner wires the guard into feature_getter."""
    runner = BacktestRunner(portfolio_id=uuid.uuid4())
    ctx = runner._build_context(
        trading_date=date(2026, 4, 27),
        backtest_run_id=uuid.uuid4(),
    )
    # The guard's bound method has __self__ set to the LookaheadGuard.
    bound_self = getattr(ctx.feature_getter, "__self__", None)
    assert bound_self is not None
    from src.quant.backtest.checks.lookahead import LookaheadGuard
    assert isinstance(bound_self, LookaheadGuard)


def test_runner_lookahead_guard_disabled_via_flag():
    runner = BacktestRunner(
        portfolio_id=uuid.uuid4(),
        enable_lookahead_guard=False,
    )
    ctx = runner._build_context(
        trading_date=date(2026, 4, 27),
        backtest_run_id=uuid.uuid4(),
    )
    # No guard wrapping → feature_getter is the raw BacktestFeatureStore.get
    bound_self = getattr(ctx.feature_getter, "__self__", None)
    from src.quant.backtest.checks.lookahead import LookaheadGuard
    assert not isinstance(bound_self, LookaheadGuard)


# ---------------------------------------------------------------------------
# _maybe_tqdm generator-safety (M-rev2 fix)
# ---------------------------------------------------------------------------

def test_maybe_tqdm_does_not_exhaust_generators():
    """A generator passed in must not be consumed before tqdm sees it.

    Pre-fix, ``len(list(iterable))`` exhausted generators, leaving tqdm to
    wrap an empty iterator. The fix uses ``len(iterable)`` only when
    ``__len__`` is available.
    """
    from src.quant.backtest.runner import _maybe_tqdm

    def _gen():
        for i in range(5):
            yield i

    g = _gen()
    out = _maybe_tqdm(g, enabled=True, desc="test")
    # Whether tqdm is installed or not, the iterable's first element must
    # still be reachable — the fix guarantees no upstream consumption.
    items = list(out)
    assert items == [0, 1, 2, 3, 4]


def test_maybe_tqdm_passthrough_when_disabled():
    from src.quant.backtest.runner import _maybe_tqdm
    src = [1, 2, 3]
    out = _maybe_tqdm(src, enabled=False, desc="test")
    # Returns the same object — pure passthrough.
    assert out is src


def test_maybe_tqdm_handles_lists_with_known_total():
    from src.quant.backtest.runner import _maybe_tqdm
    src = [date(2026, 4, 27), date(2026, 4, 28)]
    out = _maybe_tqdm(src, enabled=True, desc="test")
    # Both with and without tqdm installed, iteration must be intact.
    assert list(out) == src


# ---------------------------------------------------------------------------
# _read_run_summary uses requested trading_date in fallback (L-rev1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_run_summary_fallback_uses_requested_trading_date(monkeypatch):
    """When the row isn't found, the fallback must surface the requested
    ``trading_date`` rather than ``date.today()`` so reports don't
    misattribute the failure to the wrong calendar day.
    """
    from contextlib import asynccontextmanager

    class _NoRowSession:
        async def get(self, _model, _key):
            return None

    @asynccontextmanager
    async def _scope():
        yield _NoRowSession()

    monkeypatch.setattr("src.quant.backtest.runner.session_scope", _scope)

    requested = date(2026, 4, 27)
    result = await BacktestRunner._read_run_summary(
        uuid.uuid4(), trading_date=requested
    )
    assert result.failed is True
    assert result.backtest_date == requested
    assert "row not found" in (result.error or "")


# ---------------------------------------------------------------------------
# run_range — orchestration logic with mocked orchestrator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_range_enumerates_trading_days(monkeypatch):
    runner = BacktestRunner(portfolio_id=uuid.uuid4())

    runner._fetch_nav = AsyncMock(return_value=1_000_000.0)
    create_calls: list[date] = []

    async def _fake_create(*, trading_date, starting_nav, universe):
        create_calls.append(trading_date)
        return uuid.uuid4()

    runner._create_backtest_run_row = _fake_create

    runloop_calls: list = []

    async def _fake_run_loop(portfolio_id, *, as_of=None, ctx=None, **_kw):
        runloop_calls.append((portfolio_id, as_of, ctx.mode if ctx else None))

    monkeypatch.setattr(
        "src.quant.backtest.runner.orchestrator.run_loop", _fake_run_loop
    )

    async def _fake_summary(run_id, *, trading_date):
        return SingleDayResult(
            backtest_date=trading_date,
            backtest_run_id=run_id,
            starting_nav=1e6,
            final_nav=1.001e6,
            pnl_pct=0.001,
            trade_count=2,
        )

    runner._read_run_summary = _fake_summary

    # 2026-04-27 (Mon) → 2026-04-29 (Wed). 3 trading days.
    result = await runner.run_range(date(2026, 4, 27), date(2026, 4, 29))

    assert len(create_calls) == 3
    assert len(runloop_calls) == 3
    assert all(mode == "backtest" for _, _, mode in runloop_calls)
    assert result.n_days == 3


@pytest.mark.asyncio
async def test_run_range_skips_weekends(monkeypatch):
    runner = BacktestRunner(portfolio_id=uuid.uuid4())
    runner._fetch_nav = AsyncMock(return_value=1.0e6)
    create_dates: list[date] = []

    async def _fake_create(*, trading_date, starting_nav, universe):
        create_dates.append(trading_date)
        return uuid.uuid4()

    runner._create_backtest_run_row = _fake_create
    monkeypatch.setattr(
        "src.quant.backtest.runner.orchestrator.run_loop", AsyncMock()
    )
    runner._read_run_summary = AsyncMock(
        return_value=SingleDayResult(
            backtest_date=date(2026, 5, 1),
            backtest_run_id=uuid.uuid4(),
            starting_nav=1e6, final_nav=1e6, pnl_pct=0.0, trade_count=0,
        )
    )

    # Fri 2026-05-01 → Mon 2026-05-04. Skips Sat 5/2 and Sun 5/3.
    await runner.run_range(date(2026, 5, 1), date(2026, 5, 4))
    assert create_dates == [date(2026, 5, 1), date(2026, 5, 4)]


@pytest.mark.asyncio
async def test_run_range_per_day_error_does_not_abort(monkeypatch):
    runner = BacktestRunner(portfolio_id=uuid.uuid4())
    runner._fetch_nav = AsyncMock(return_value=1.0e6)
    runner._create_backtest_run_row = AsyncMock(side_effect=lambda **_: uuid.uuid4())

    side = [None, RuntimeError("transient day-2 error"), None]
    rl_mock = AsyncMock(side_effect=side)
    monkeypatch.setattr("src.quant.backtest.runner.orchestrator.run_loop", rl_mock)
    runner._read_run_summary = AsyncMock(
        return_value=SingleDayResult(
            backtest_date=date(2026, 4, 27), backtest_run_id=uuid.uuid4(),
            starting_nav=1e6, final_nav=1e6, pnl_pct=0.0, trade_count=0,
        )
    )

    result = await runner.run_range(date(2026, 4, 27), date(2026, 4, 29))
    assert result.n_days == 3
    assert result.n_failed == 1
    failed_day = next(d for d in result.days if d.failed)
    assert "transient day-2 error" in failed_day.error


@pytest.mark.asyncio
async def test_run_range_empty_for_weekend_only_range(monkeypatch):
    runner = BacktestRunner(portfolio_id=uuid.uuid4())
    fetch_mock = AsyncMock()
    runner._fetch_nav = fetch_mock
    result = await runner.run_range(date(2026, 5, 2), date(2026, 5, 3))
    assert result.n_days == 0
    assert fetch_mock.await_count == 0


@pytest.mark.asyncio
async def test_run_range_respects_holidays(monkeypatch):
    holiday = date(2026, 4, 28)  # Tue
    runner = BacktestRunner(
        portfolio_id=uuid.uuid4(),
        holidays={holiday},
    )
    runner._fetch_nav = AsyncMock(return_value=1.0e6)
    create_dates: list[date] = []

    async def _fake_create(*, trading_date, starting_nav, universe):
        create_dates.append(trading_date)
        return uuid.uuid4()

    runner._create_backtest_run_row = _fake_create
    monkeypatch.setattr(
        "src.quant.backtest.runner.orchestrator.run_loop", AsyncMock()
    )
    runner._read_run_summary = AsyncMock(
        return_value=SingleDayResult(
            backtest_date=holiday, backtest_run_id=uuid.uuid4(),
            starting_nav=1e6, final_nav=1e6, pnl_pct=0.0, trade_count=0,
        )
    )
    await runner.run_range(date(2026, 4, 27), date(2026, 4, 29))
    assert holiday not in create_dates
    assert create_dates == [date(2026, 4, 27), date(2026, 4, 29)]


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def test_cli_parser_accepts_well_formed_args():
    from scripts.backtest_run import build_parser

    parser = build_parser()
    pid = str(uuid.uuid4())
    args = parser.parse_args(
        [
            "--start-date", "2026-04-27",
            "--end-date", "2026-04-29",
            "--portfolio-id", pid,
            "--seed", "7",
            "--smile-method", "flat",
        ]
    )
    assert args.start_date == date(2026, 4, 27)
    assert args.end_date == date(2026, 4, 29)
    assert str(args.portfolio_id) == pid
    assert args.seed == 7
    assert args.smile_method == "flat"


def test_cli_parser_rejects_invalid_uuid():
    from scripts.backtest_run import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--start-date", "2026-04-27",
                "--end-date", "2026-04-29",
                "--portfolio-id", "not-a-uuid",
            ]
        )


def test_cli_main_rejects_inverted_dates(monkeypatch):
    from scripts import backtest_run

    monkeypatch.setattr(
        backtest_run, "main_async", AsyncMock(return_value=0)
    )
    pid = str(uuid.uuid4())
    with pytest.raises(SystemExit):
        backtest_run.main(
            [
                "--start-date", "2026-04-30",
                "--end-date", "2026-04-27",
                "--portfolio-id", pid,
            ]
        )


def test_cli_parser_defaults():
    from scripts.backtest_run import build_parser

    parser = build_parser()
    pid = str(uuid.uuid4())
    args = parser.parse_args(
        ["--start-date", "2026-04-27", "--end-date", "2026-04-27", "--portfolio-id", pid]
    )
    assert args.seed == 42
    assert args.smile_method is None
    assert args.risk_free_rate is None
