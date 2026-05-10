"""Tests for ``scripts.analyst_pack`` — pure helpers only.

The async I/O orchestration in ``main_async`` is exercised by the manual
end-to-end smoke run against real DB data — this test file covers the
functions a researcher might one day rip out and reuse:
``_compute_arm_stats``, ``_pick_trace_samples``, ``_aggregate_funnel_buckets``,
``_trades_csv_text``.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.analyst_pack import (
    _aggregate_funnel_buckets,
    _compute_arm_stats,
    _pick_trace_samples,
    _trades_csv_text,
)
from src.quant.inspector import (
    RunMetadata, SessionSkeleton, TickSummary, TradeRecord, UniverseEntry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _trade(
    arm_id: str = "X_momentum",
    primitive: str = "momentum",
    pnl: float | None = 100.0,
    lots: int = 2,
    entry_premium: float = 50.0,
    *,
    minutes_held: int = 15,
    direction: str = "bullish",
) -> TradeRecord:
    entry = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    exit_ = entry + timedelta(minutes=minutes_held) if pnl is not None else None
    return TradeRecord(
        trade_id=uuid.uuid4(),
        arm_id=arm_id,
        primitive_name=primitive,
        underlying_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        direction=direction,
        entry_at=entry,
        exit_at=exit_,
        entry_premium_net=entry_premium,
        exit_premium_net=(entry_premium + (pnl / lots)) if pnl is not None else None,
        realized_pnl=pnl,
        lots=lots,
        exit_reason="trailing_stop" if pnl is not None else None,
    )


def _tick_summary(*, opened=0, lost_bandit=0, weak_total=0, weak_strong=0, **rest):
    """Builds a TickSummary-shaped namespace.

    ``weak_total`` = total signals incl. weak; ``weak_strong`` = strong
    (non-weak) — the difference is the weak_signal count.
    """
    return TickSummary(
        virtual_time=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        n_signals_total=weak_total or (opened + lost_bandit + rest.get("sized_zero", 0) + rest.get("cooloff", 0)),
        n_signals_strong=weak_strong if weak_strong else (opened + lost_bandit + rest.get("sized_zero", 0) + rest.get("cooloff", 0)),
        n_opened=opened,
        n_lost_bandit=lost_bandit,
        n_sized_zero=rest.get("sized_zero", 0),
        n_cooloff=rest.get("cooloff", 0),
        n_kill_switch=rest.get("kill_switch", 0),
        n_capacity_full=rest.get("capacity_full", 0),
        n_warmup=rest.get("warmup", 0),
    )


def _skeleton(ticks: list[TickSummary]) -> SessionSkeleton:
    md = RunMetadata(
        run_id=uuid.uuid4(),
        portfolio_id=uuid.uuid4(),
        backtest_date=datetime(2026, 5, 8).date(),
        started_at=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        starting_nav=1_000_000.0,
        final_nav=1_010_000.0,
        pnl_pct=0.01,
        trade_count=5,
        bandit_seed=42,
    )
    return SessionSkeleton(
        metadata=md, universe=[], config_snapshot={}, ticks=ticks, trades=[],
    )


# ---------------------------------------------------------------------------
# _compute_arm_stats
# ---------------------------------------------------------------------------

def test_compute_arm_stats_groups_by_arm_and_sorts_by_pnl():
    trades = [
        _trade(arm_id="A", pnl=100.0),
        _trade(arm_id="A", pnl=50.0),
        _trade(arm_id="B", pnl=200.0),
        _trade(arm_id="C", pnl=-30.0),
    ]
    out = _compute_arm_stats(trades)
    arm_ids = [s.arm_id for s in out]
    assert arm_ids == ["B", "A", "C"]  # sorted by total_pnl desc
    a = next(s for s in out if s.arm_id == "A")
    assert a.n_trades == 2
    assert a.total_pnl == 150.0
    assert a.win_rate == 1.0


def test_compute_arm_stats_skips_open_trades():
    """Trades with no realized_pnl don't poison aggregates."""
    trades = [
        _trade(arm_id="A", pnl=50.0),
        _trade(arm_id="A", pnl=None),  # open
    ]
    out = _compute_arm_stats(trades)
    assert len(out) == 1
    assert out[0].n_trades == 1


def test_compute_arm_stats_profit_factor_handles_zero_losses():
    """All wins → profit factor approaches infinity (div by ~0); display
    helper renders ∞."""
    trades = [_trade(arm_id="A", pnl=100.0), _trade(arm_id="A", pnl=50.0)]
    out = _compute_arm_stats(trades)
    # PF = 150 / 1e-9 → very large
    assert out[0].profit_factor > 1e6


def test_compute_arm_stats_win_rate_correct_with_mixed_outcomes():
    trades = [
        _trade(arm_id="A", pnl=100.0),
        _trade(arm_id="A", pnl=-50.0),
        _trade(arm_id="A", pnl=25.0),
    ]
    out = _compute_arm_stats(trades)
    assert out[0].n_wins == 2
    assert out[0].win_rate == pytest.approx(2 / 3)


def test_compute_arm_stats_avg_holding_minutes_uses_only_closed_trades():
    trades = [
        _trade(arm_id="A", pnl=100.0, minutes_held=10),
        _trade(arm_id="A", pnl=50.0, minutes_held=20),
    ]
    out = _compute_arm_stats(trades)
    assert out[0].avg_holding_minutes == pytest.approx(15.0)


def test_compute_arm_stats_empty_input():
    assert _compute_arm_stats([]) == []


# ---------------------------------------------------------------------------
# _pick_trace_samples
# ---------------------------------------------------------------------------

def test_pick_trace_samples_returns_top_winners_and_worst_losers():
    trades = [
        _trade(arm_id="A", pnl=100.0),    # winner
        _trade(arm_id="B", pnl=-200.0),   # worst loser
        _trade(arm_id="C", pnl=50.0),     # winner
        _trade(arm_id="D", pnl=-50.0),    # mid loser
        _trade(arm_id="E", pnl=10.0),     # small winner
    ]
    picks = _pick_trace_samples(trades, k_winners=2, k_losers=2)
    pnls = sorted(float(t.realized_pnl) for t in picks)
    # Two largest + two smallest
    assert 100.0 in pnls and 50.0 in pnls
    assert -200.0 in pnls and -50.0 in pnls


def test_pick_trace_samples_skips_open_trades():
    trades = [
        _trade(arm_id="A", pnl=100.0),
        _trade(arm_id="B", pnl=None),  # open — must be excluded
    ]
    picks = _pick_trace_samples(trades, k_winners=2, k_losers=2)
    assert all(t.realized_pnl is not None for t in picks)


def test_pick_trace_samples_dedupes_when_k_exceeds_pool():
    """If k_winners + k_losers > n_trades, no trade should appear twice."""
    trades = [_trade(arm_id="A", pnl=100.0), _trade(arm_id="B", pnl=-50.0)]
    picks = _pick_trace_samples(trades, k_winners=5, k_losers=5)
    ids = [t.trade_id for t in picks]
    assert len(ids) == len(set(ids))
    assert len(picks) == 2


def test_pick_trace_samples_empty_returns_empty():
    assert _pick_trace_samples([], k_winners=3, k_losers=3) == []


def test_pick_trace_samples_zero_losers_returns_only_winners():
    trades = [_trade(arm_id="A", pnl=100.0), _trade(arm_id="B", pnl=-50.0)]
    picks = _pick_trace_samples(trades, k_winners=1, k_losers=0)
    assert len(picks) == 1
    assert float(picks[0].realized_pnl) == 100.0


# ---------------------------------------------------------------------------
# _aggregate_funnel_buckets
# ---------------------------------------------------------------------------

def test_aggregate_funnel_buckets_sums_across_skeletons_and_ticks():
    ticks_day1 = [
        _tick_summary(weak_total=10, weak_strong=8, opened=2, lost_bandit=6),
        _tick_summary(weak_total=5, weak_strong=5, opened=1, lost_bandit=4),
    ]
    ticks_day2 = [
        _tick_summary(weak_total=8, weak_strong=6, opened=0, lost_bandit=6),
    ]
    skels = [_skeleton(ticks_day1), _skeleton(ticks_day2)]
    counts = _aggregate_funnel_buckets(skels)
    assert counts["opened"] == 3
    assert counts["lost_bandit"] == 16
    # weak_signal is derived as (total - strong) per tick: (10-8) + (5-5) + (8-6) = 4
    assert counts["weak_signal"] == 4


def test_aggregate_funnel_buckets_empty():
    assert _aggregate_funnel_buckets([]) == {}


# ---------------------------------------------------------------------------
# _trades_csv_text
# ---------------------------------------------------------------------------

def test_trades_csv_includes_header_and_one_row_per_trade():
    skel = _skeleton([])
    trades_with_meta = [
        (skel, _trade(pnl=100.0, lots=2, entry_premium=50.0), "RELIANCE"),
        (skel, _trade(pnl=-30.0, lots=1, entry_premium=20.0), "TCS"),
    ]
    text = _trades_csv_text(trades_with_meta)
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 3  # header + 2 data rows
    header = rows[0]
    assert "trade_id" in header
    assert "realized_pnl" in header
    assert "symbol" in header
    # First data row carries the symbol
    assert "RELIANCE" in rows[1]


def test_trades_csv_handles_open_trades_with_blank_exit_columns():
    skel = _skeleton([])
    trades_with_meta = [(skel, _trade(pnl=None), "RELIANCE")]
    text = _trades_csv_text(trades_with_meta)
    rows = list(csv.reader(io.StringIO(text)))
    data_row = rows[1]
    header = rows[0]
    realized_idx = header.index("realized_pnl")
    exit_idx = header.index("exit_at_utc")
    assert data_row[realized_idx] == ""
    assert data_row[exit_idx] == ""
