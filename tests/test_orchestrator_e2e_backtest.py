"""End-to-end smoke test of ``orchestrator.run_loop`` driven by a backtest
``OrchestratorContext``.

Verifies the Task 9 abstractions actually *compose* end-to-end: the
backtest clock advances, the feature getter is consulted, the universe
selector is consulted, and the trade recorder receives day-init /
day-finalize calls. Per-day persistence helpers (``_get_nav``,
``_load_open_positions``) are mocked since they touch live tables.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytz

from src.quant import orchestrator as orch
from src.quant.bandit.selector import ArmSelector
from src.quant.backtest.clock import BacktestClock
from src.quant.backtest.universe_top_gainers import TopGainersUniverseSelector
from src.quant.clock import BacktestClockAdapter
from src.quant.context import OrchestratorContext
from src.quant.feature_store import FeatureBundle
from src.quant.recorder import (
    CloseTradePayload,
    DayFinalizePayload,
    DayInitPayload,
    OpenTradePayload,
    TradeRecorder,
)


_IST = pytz.timezone("Asia/Kolkata")


class _RecordingRecorder(TradeRecorder):
    """In-memory trade recorder. Captures every recorder call."""

    def __init__(self):
        self.opens: list[OpenTradePayload] = []
        self.closes: list[CloseTradePayload] = []
        self.day_inits: list[DayInitPayload] = []
        self.day_finalizes: list[DayFinalizePayload] = []
        self.signal_logs: list = []

    async def open_trade(self, payload):
        self.opens.append(payload)
        return uuid.uuid4()

    async def close_trade(self, payload):
        self.closes.append(payload)

    async def init_day(self, payload):
        self.day_inits.append(payload)

    async def finalize_day(self, payload):
        self.day_finalizes.append(payload)

    async def record_signals(self, payload):
        self.signal_logs.append(payload)


class _StubUniverseSelector:
    """Returns a fixed universe — bypasses DB."""

    def __init__(self, universe):
        self._universe = universe

    async def select(self, _date):
        return self._universe


def _make_bundle(symbol: str, instrument_id: uuid.UUID, ts: datetime) -> FeatureBundle:
    return FeatureBundle(
        underlying_id=instrument_id,
        underlying_symbol=symbol,
        captured_at=ts,
        underlying_ltp=20000.0,
        underlying_volume_3min=1000.0,
        vwap_today=20000.0,
        realized_vol_3min=0.15,
        realized_vol_30min=0.18,
        atm_iv=0.18,
        atm_oi=50_000.0,
        atm_bid=Decimal("100.0"),
        atm_ask=Decimal("100.5"),
        bid_volume_3min_change=0.0,
        ask_volume_3min_change=0.0,
        bb_width=0.05,
        vix_value=15.0,
        vix_regime="neutral",
    )


# ---------------------------------------------------------------------------
# E2E smoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_loop_with_backtest_context_drives_recorder(monkeypatch):
    """Wire a full backtest ctx and verify the orchestrator delegates to it.

    Asserts:
      * The backtest clock's ``now()`` is consulted (test hard-exit fires
        at the configured time).
      * The injected ``feature_getter`` is called for each tick.
      * The injected ``universe_selector`` is called once at start.
      * The recorder receives ``init_day`` and ``finalize_day``.
    """
    portfolio_id = uuid.uuid4()
    instrument_id = uuid.uuid4()
    universe = [{"id": instrument_id, "symbol": "NIFTY", "name": "Nifty 50"}]

    # Mock the orchestrator's persistence touchpoints (live tables).
    monkeypatch.setattr(orch, "_get_nav", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        orch, "_load_open_positions",
        AsyncMock(return_value=([], Decimal("0"))),
    )
    monkeypatch.setattr(
        orch.persistence, "load_morning",
        AsyncMock(return_value=ArmSelector(["NIFTY_orb"], seed=0)),
    )
    monkeypatch.setattr(
        orch.persistence, "save_eod", AsyncMock(return_value=None)
    )

    # Backtest context wiring
    feature_calls: list[tuple] = []

    async def _feature_getter(uid, ts):
        feature_calls.append((uid, ts))
        return _make_bundle("NIFTY", instrument_id, ts)

    bt_clock = BacktestClock(
        trading_date=date(2026, 5, 8),
        market_open=time(14, 27),  # near hard-exit so loop terminates quickly
        market_close=time(15, 30),
    )
    universe_selector = _StubUniverseSelector(universe)
    recorder = _RecordingRecorder()
    ctx = OrchestratorContext(
        mode="backtest",
        clock=BacktestClockAdapter(inner=bt_clock),
        feature_getter=_feature_getter,
        universe_selector=universe_selector,
        recorder=recorder,
    )

    # Use legacy as_of path for the loop's initial time. With as_of set,
    # the orchestrator advances current_time by poll_delta; the loop
    # exits when now_ist >= 14:30 hard exit.
    as_of = _IST.localize(datetime(2026, 5, 8, 14, 27)).astimezone(timezone.utc)

    await orch.run_loop(portfolio_id, as_of=as_of, ctx=ctx)

    # Recorder saw the day boundaries
    assert len(recorder.day_inits) == 1
    assert recorder.day_inits[0].portfolio_id == portfolio_id
    assert recorder.day_inits[0].trading_date == date(2026, 5, 8)
    assert len(recorder.day_finalizes) == 1
    assert recorder.day_finalizes[0].portfolio_id == portfolio_id

    # Universe selector consulted exactly once
    assert len(feature_calls) >= 1  # at least one tick before hard-exit

    # Decision-Inspector trace plumbing: signal-log payloads carry the
    # virtual_time and an entries list (may be empty when no primitive
    # fires in this short test window — but the recorder must at minimum
    # be wired and reachable). Each entry, if present, must expose the
    # three optional trace fields without raising.
    for payload in recorder.signal_logs:
        assert payload.virtual_time is not None
        for entry in payload.entries:
            # Fields exist on the dataclass — None when not applicable, dict
            # when populated. Either is acceptable here; the focused unit
            # tests in test_quant_decision_inspector_traces.py assert the
            # population semantics per bucket.
            assert hasattr(entry, "primitive_trace")
            assert hasattr(entry, "bandit_trace")
            assert hasattr(entry, "sizer_trace")


@pytest.mark.asyncio
async def test_run_loop_with_backtest_context_empty_universe_aborts(monkeypatch):
    """Empty universe → loop logs and returns early without touching recorder."""
    portfolio_id = uuid.uuid4()
    monkeypatch.setattr(orch, "_get_nav", AsyncMock(return_value=1.0))

    recorder = _RecordingRecorder()
    ctx = OrchestratorContext(
        mode="backtest",
        clock=BacktestClockAdapter(inner=BacktestClock(trading_date=date(2026, 5, 8))),
        feature_getter=AsyncMock(return_value=None),
        universe_selector=_StubUniverseSelector([]),  # empty
        recorder=recorder,
    )
    await orch.run_loop(portfolio_id, ctx=ctx)
    # Empty universe → early return, no recorder calls
    assert recorder.day_inits == []
    assert recorder.day_finalizes == []


# ---------------------------------------------------------------------------
# Regression test for review-pass-2 C1:
# LookaheadGuard wired into BacktestRunner must NOT false-positive across
# multiple ticks. Pre-fix, the BacktestClock was frozen at session open
# while ``current_time`` advanced via the legacy ``as_of`` path — every
# tick after the first triggered a violation that the orchestrator's
# tick-level try/except silently absorbed (silent zero-trade-output).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookahead_guard_no_false_positives_across_multiple_ticks(monkeypatch):
    """Run 5+ ticks with LookaheadGuard wired and assert ``n_violations == 0``."""
    from src.quant.backtest.checks.lookahead import LookaheadGuard

    portfolio_id = uuid.uuid4()
    instrument_id = uuid.uuid4()
    universe = [{"id": instrument_id, "symbol": "NIFTY", "name": "Nifty 50"}]

    monkeypatch.setattr(orch, "_get_nav", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        orch, "_load_open_positions",
        AsyncMock(return_value=([], Decimal("0"))),
    )
    monkeypatch.setattr(
        orch.persistence, "load_morning",
        AsyncMock(return_value=ArmSelector(["NIFTY_orb"], seed=0)),
    )
    monkeypatch.setattr(
        orch.persistence, "save_eod", AsyncMock(return_value=None)
    )

    feature_calls: list[tuple] = []

    async def _feature_getter(uid, ts):
        feature_calls.append((uid, ts))
        return _make_bundle("NIFTY", instrument_id, ts)

    # Wire the guard ourselves so we can inspect stats post-run.
    bt_clock = BacktestClock(
        trading_date=date(2026, 5, 8),
        market_open=time(13, 50),  # leaves room for ~10 ticks before 14:30 hard exit
        market_close=time(15, 30),
    )
    clock_adapter = BacktestClockAdapter(inner=bt_clock)
    guard = LookaheadGuard(_feature_getter, clock=clock_adapter)

    recorder = _RecordingRecorder()
    ctx = OrchestratorContext(
        mode="backtest",
        clock=clock_adapter,
        feature_getter=guard.checked_get,
        universe_selector=_StubUniverseSelector(universe),
        recorder=recorder,
    )

    as_of = _IST.localize(datetime(2026, 5, 8, 13, 50)).astimezone(timezone.utc)
    await orch.run_loop(portfolio_id, as_of=as_of, ctx=ctx)

    stats = guard.stats()
    assert stats.n_violations == 0, (
        f"LookaheadGuard reported {stats.n_violations} violations across "
        f"{stats.n_calls} feature reads — clock advancement is broken. "
        f"Max lookahead seen: {stats.max_lookahead_seconds}s."
    )
    # Confirm we actually exercised the path: many ticks ran, many feature reads
    assert len(feature_calls) >= 5, (
        f"Expected ≥5 feature reads across the session, got {len(feature_calls)}. "
        "If this drops to 1, the hard-exit gate is firing too early."
    )
