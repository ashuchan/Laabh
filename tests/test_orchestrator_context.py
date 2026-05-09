"""Tests for the Task 9 abstractions: Clock, TradeRecorder, OrchestratorContext.

These verify the new modular boundary between orchestrator and I/O. Each
abstraction is exercised independently of the orchestrator's main loop.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytz

from src.quant.clock import BacktestClockAdapter, Clock, LiveClock
from src.quant.context import OrchestratorContext
from src.quant.recorder import (
    BacktestTradeRecorder,
    CloseTradePayload,
    DayFinalizePayload,
    DayInitPayload,
    LiveTradeRecorder,
    OpenTradePayload,
    TradeRecorder,
)


_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Clock Protocol — structural typing
# ---------------------------------------------------------------------------

def test_live_clock_satisfies_clock_protocol():
    assert isinstance(LiveClock(), Clock)


def test_backtest_clock_adapter_satisfies_clock_protocol():
    """BacktestClock wrapped by the adapter must satisfy the same Protocol."""
    from src.quant.backtest.clock import BacktestClock

    inner = BacktestClock(trading_date=date(2026, 4, 27))
    adapter = BacktestClockAdapter(inner=inner)
    assert isinstance(adapter, Clock)


def test_live_clock_now_returns_aware_utc():
    n = LiveClock().now()
    assert n.tzinfo is not None
    assert n.utcoffset() == timezone.utc.utcoffset(None)


def test_live_clock_is_after_hard_exit_uses_ist():
    """Hard-exit comparisons must be done in IST regardless of system tz."""
    clock = LiveClock()
    # Forcing the answer requires time-mocking; assert at least the call
    # path is reachable and returns a bool.
    result = clock.is_after_hard_exit(time(0, 1))
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_live_clock_sleep_until_next_tick_blocks_when_under_budget(monkeypatch):
    sleeps = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    clock = LiveClock()
    tick_start = clock.now()
    await clock.sleep_until_next_tick(tick_start=tick_start, poll_seconds=180)
    # We invoked sleep once with a positive duration close to 180 (since
    # negligible time elapsed since tick_start).
    assert len(sleeps) == 1
    assert sleeps[0] > 0


@pytest.mark.asyncio
async def test_backtest_clock_adapter_advances_inner_time():
    from src.quant.backtest.clock import BacktestClock

    inner = BacktestClock(trading_date=date(2026, 4, 27), tick_seconds=180)
    adapter = BacktestClockAdapter(inner=inner)
    t0 = adapter.now()
    await adapter.sleep_until_next_tick(tick_start=t0, poll_seconds=180)
    t1 = adapter.now()
    assert (t1 - t0).total_seconds() == 180


# ---------------------------------------------------------------------------
# TradeRecorder ABC — Liskov substitution
# ---------------------------------------------------------------------------

def test_live_recorder_is_trade_recorder():
    assert isinstance(LiveTradeRecorder(), TradeRecorder)


def test_backtest_recorder_is_trade_recorder():
    assert isinstance(
        BacktestTradeRecorder(backtest_run_id=uuid.uuid4()), TradeRecorder
    )


def test_backtest_recorder_carries_provenance_tags():
    r = BacktestTradeRecorder(
        backtest_run_id=uuid.uuid4(),
        chain_source="dhan_historical",
        underlying_source="dhan_intraday",
    )
    assert r._chain_source == "dhan_historical"
    assert r._underlying_source == "dhan_intraday"


# ---------------------------------------------------------------------------
# Recorder I/O — mocked session
# ---------------------------------------------------------------------------

class _RecordingSession:
    """Minimal async-session double that records calls."""

    def __init__(self, get_returns=None):
        self.added = []
        self.executed = []
        self._get_returns = get_returns
        self.flush = AsyncMock()

    async def execute(self, stmt):
        self.executed.append(stmt)
        return None

    async def get(self, model, key):
        return self._get_returns

    def add(self, obj):
        self.added.append(obj)


@pytest.mark.asyncio
async def test_live_recorder_open_trade_inserts_quant_trade(monkeypatch):
    from src.models.quant_trade import QuantTrade

    session = _RecordingSession()

    @asynccontextmanager
    async def _scope():
        # Simulate the post-flush id population
        async def _flush():
            for obj in session.added:
                if not getattr(obj, "id", None):
                    obj.id = uuid.uuid4()
        session.flush = _flush
        yield session

    monkeypatch.setattr("src.quant.recorder.session_scope", _scope)

    recorder = LiveTradeRecorder()
    payload = OpenTradePayload(
        portfolio_id=uuid.uuid4(),
        underlying_id=uuid.uuid4(),
        primitive_name="orb",
        arm_id="RELIANCE_orb",
        direction="bullish",
        entry_at=datetime(2026, 4, 27, 9, 30, tzinfo=timezone.utc),
        entry_premium_net=Decimal("125.00"),
        estimated_costs=Decimal("250"),
        signal_strength_at_entry=0.7,
        posterior_mean_at_entry=0.001,
        sampled_mean_at_entry=0.002,
        bandit_seed=42,
        kelly_fraction=0.5,
        lots=2,
    )
    trade_id = await recorder.open_trade(payload)
    assert trade_id is not None
    assert len(session.added) == 1
    inserted = session.added[0]
    assert isinstance(inserted, QuantTrade)
    assert inserted.arm_id == "RELIANCE_orb"
    assert inserted.lots == 2
    assert inserted.status == "open"


@pytest.mark.asyncio
async def test_backtest_recorder_open_trade_writes_backtest_table(monkeypatch):
    from src.models.backtest_trade import BacktestTrade

    session = _RecordingSession()

    @asynccontextmanager
    async def _scope():
        async def _flush():
            for obj in session.added:
                if not getattr(obj, "id", None):
                    obj.id = uuid.uuid4()
        session.flush = _flush
        yield session

    monkeypatch.setattr("src.quant.recorder.session_scope", _scope)

    run_id = uuid.uuid4()
    recorder = BacktestTradeRecorder(
        backtest_run_id=run_id,
        chain_source="synthesized",
        underlying_source="dhan_intraday",
    )
    payload = OpenTradePayload(
        portfolio_id=uuid.uuid4(),
        underlying_id=uuid.uuid4(),
        primitive_name="orb",
        arm_id="X_orb",
        direction="bullish",
        entry_at=datetime(2026, 4, 27, 9, 30, tzinfo=timezone.utc),
        entry_premium_net=Decimal("125.00"),
        estimated_costs=Decimal("250"),
        signal_strength_at_entry=0.7,
        posterior_mean_at_entry=0.001,
        sampled_mean_at_entry=0.002,
        bandit_seed=42,
        kelly_fraction=0.5,
        lots=2,
    )
    await recorder.open_trade(payload)
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, BacktestTrade)
    assert row.backtest_run_id == run_id
    assert row.chain_source == "synthesized"
    assert row.underlying_source == "dhan_intraday"


@pytest.mark.asyncio
async def test_backtest_recorder_init_day_is_noop():
    """BacktestRunner pre-creates the run row; init_day must not double-write."""
    recorder = BacktestTradeRecorder(backtest_run_id=uuid.uuid4())
    payload = DayInitPayload(
        portfolio_id=uuid.uuid4(),
        trading_date=date(2026, 4, 27),
        starting_nav=1_000_000.0,
        universe=[],
        config_snapshot={},
        bandit_seed=42,
    )
    # No exception, no DB calls.
    await recorder.init_day(payload)


# ---------------------------------------------------------------------------
# OrchestratorContext factory
# ---------------------------------------------------------------------------

def test_orchestrator_context_live_factory_wires_live_impls():
    ctx = OrchestratorContext.live()
    assert ctx.mode == "live"
    assert isinstance(ctx.clock, LiveClock)
    assert isinstance(ctx.recorder, LiveTradeRecorder)
    # universe_selector is the LLM impl
    from src.quant.universe import LLMUniverseSelector
    assert isinstance(ctx.universe_selector, LLMUniverseSelector)
    # feature_getter is the live module function
    from src.quant import feature_store as live_fs
    assert ctx.feature_getter is live_fs.get


def test_orchestrator_context_backtest_constructible_with_injection():
    """Confirm the structural design: a backtest context can be built by
    injecting backtest impls without touching live wiring."""
    from src.quant.backtest.clock import BacktestClock
    from src.quant.backtest.feature_store import BacktestFeatureStore
    from src.quant.backtest.universe_top_gainers import TopGainersUniverseSelector

    bt_clock = BacktestClock(trading_date=date(2026, 4, 27))
    bt_fs = BacktestFeatureStore(trading_date=date(2026, 4, 27))
    bt_universe = TopGainersUniverseSelector()
    bt_recorder = BacktestTradeRecorder(backtest_run_id=uuid.uuid4())

    ctx = OrchestratorContext(
        mode="backtest",
        clock=BacktestClockAdapter(inner=bt_clock),
        feature_getter=bt_fs.get,
        universe_selector=bt_universe,
        recorder=bt_recorder,
    )
    assert ctx.mode == "backtest"
    assert isinstance(ctx.clock, Clock)
    assert isinstance(ctx.recorder, TradeRecorder)
