"""Unit tests for the walk-forward harness.

Window math is tested directly. The end-to-end ``run_walk_forward`` path is
tested with a mocked runner.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.quant.backtest.reporting.walk_forward import (
    WalkForwardResult,
    WalkForwardWindow,
    compute_windows,
    run_walk_forward,
    windows_have_no_overlapping_tests,
)


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------

def test_compute_windows_default_slide_no_overlap():
    """With default slide_days = test_days, test ranges must not overlap."""
    windows = compute_windows(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=5,
    )
    assert len(windows) > 0
    assert windows_have_no_overlapping_tests(windows)


def test_compute_windows_purge_segment_is_between_train_and_test():
    windows = compute_windows(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=5,
    )
    for w in windows:
        assert w.train_end < w.purge_start
        assert w.purge_end < w.test_start
        # 5 trading days of purge → purge_end ≥ purge_start + (>= 5 cal days)
        assert (w.purge_end - w.purge_start).days >= 5


def test_compute_windows_one_year_yields_at_least_nine():
    """1 year of trading days ≈ 252; train+purge+test = 85; expect ~9-13 windows."""
    windows = compute_windows(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=5,
    )
    assert 8 <= len(windows) <= 13


def test_compute_windows_indices_are_sequential():
    windows = compute_windows(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=5,
    )
    assert [w.index for w in windows] == list(range(len(windows)))


def test_compute_windows_zero_purge_segments_train_test_adjacent():
    windows = compute_windows(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=0,
    )
    for w in windows:
        # No purge → test starts the trading day after train ends
        assert w.purge_start == w.test_start


def test_compute_windows_smaller_slide_more_windows():
    n_default = len(compute_windows(
        start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=5,
    ))
    n_dense = len(compute_windows(
        start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=5, slide_days=10,
    ))
    assert n_dense > n_default


def test_compute_windows_too_short_returns_empty():
    """Range smaller than train+purge+test → no windows."""
    windows = compute_windows(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),  # ~22 trading days
        train_days=60, test_days=20, purge_days=5,
    )
    assert windows == []


def test_compute_windows_skips_holidays():
    holiday = date(2025, 7, 4)
    windows = compute_windows(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        train_days=60, test_days=20, purge_days=5,
        holidays={holiday},
    )
    # No window should reference the holiday as any segment endpoint
    for w in windows:
        assert holiday not in {
            w.train_start, w.train_end, w.purge_start, w.purge_end,
            w.test_start, w.test_end,
        }


def test_compute_windows_validates_inputs():
    with pytest.raises(ValueError, match="train_days"):
        compute_windows(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            train_days=0, test_days=20, purge_days=5,
        )
    with pytest.raises(ValueError, match="purge_days"):
        compute_windows(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            train_days=60, test_days=20, purge_days=-1,
        )
    with pytest.raises(ValueError, match="slide_days"):
        compute_windows(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            train_days=60, test_days=20, purge_days=5, slide_days=0,
        )


# ---------------------------------------------------------------------------
# Overlap helper
# ---------------------------------------------------------------------------

def test_overlap_helper_detects_overlap():
    a = WalkForwardWindow(0, date(2025, 1, 1), date(2025, 3, 1),
                          date(2025, 3, 2), date(2025, 3, 6),
                          date(2025, 3, 7), date(2025, 3, 21))
    b = WalkForwardWindow(1, date(2025, 1, 15), date(2025, 3, 15),
                          date(2025, 3, 16), date(2025, 3, 20),
                          date(2025, 3, 19), date(2025, 4, 2))
    assert windows_have_no_overlapping_tests([a, b]) is False


def test_overlap_helper_passes_when_disjoint():
    a = WalkForwardWindow(0, date(2025, 1, 1), date(2025, 3, 1),
                          date(2025, 3, 2), date(2025, 3, 6),
                          date(2025, 3, 7), date(2025, 3, 21))
    b = WalkForwardWindow(1, date(2025, 4, 1), date(2025, 6, 1),
                          date(2025, 6, 2), date(2025, 6, 6),
                          date(2025, 6, 7), date(2025, 6, 21))
    assert windows_have_no_overlapping_tests([a, b]) is True


# ---------------------------------------------------------------------------
# WalkForwardResult aggregate
# ---------------------------------------------------------------------------

def _wfresult_with_sharpes(sharpes: list[float]) -> WalkForwardResult:
    from src.quant.backtest.reporting.metrics import MetricsBundle

    bundles = [
        MetricsBundle(
            n=20, mean=0, median=0, std=0.01, skew=0, kurtosis_excess=0,
            sharpe=s, deflated_sharpe=0.5,
            sharpe_ci_lower=s - 0.1, sharpe_ci_upper=s + 0.1,
            win_rate=0.5, avg_win=0.01, avg_loss=-0.01, profit_factor=1.0,
            max_drawdown=0.05, calmar=0.0,
        ) for s in sharpes
    ]
    return WalkForwardResult(windows=[], per_window_metrics=bundles)


def test_median_sharpe_aggregate():
    r = _wfresult_with_sharpes([1.0, 2.0, 3.0])
    assert r.median_sharpe == 2.0


def test_median_sharpe_even_count():
    r = _wfresult_with_sharpes([1.0, 2.0, 3.0, 4.0])
    assert r.median_sharpe == 2.5


def test_red_flag_when_median_negative():
    r = _wfresult_with_sharpes([-0.5, -1.0, -0.2])
    assert r.red_flag is True


def test_red_flag_clear_when_median_positive():
    r = _wfresult_with_sharpes([0.5, 1.0, -0.2])
    assert r.red_flag is False


def test_n_windows_zero_for_empty():
    r = _wfresult_with_sharpes([])
    assert r.n_windows == 0
    assert r.median_sharpe == 0.0


# ---------------------------------------------------------------------------
# run_walk_forward — mocked runner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_walk_forward_calls_runner_train_and_test_per_window():
    windows = [
        WalkForwardWindow(0,
                          train_start=date(2025, 1, 1), train_end=date(2025, 3, 1),
                          purge_start=date(2025, 3, 2), purge_end=date(2025, 3, 6),
                          test_start=date(2025, 3, 7), test_end=date(2025, 3, 21)),
        WalkForwardWindow(1,
                          train_start=date(2025, 1, 21), train_end=date(2025, 3, 21),
                          purge_start=date(2025, 3, 22), purge_end=date(2025, 3, 26),
                          test_start=date(2025, 3, 27), test_end=date(2025, 4, 10)),
    ]

    range_calls: list[tuple[date, date]] = []

    async def _fake_run_range(start, end):
        range_calls.append((start, end))
        # Return a result whose .days have realistic pnl_pct
        return SimpleNamespace(
            days=[SimpleNamespace(pnl_pct=0.001) for _ in range(15)]
        )

    runner = SimpleNamespace(run_range=_fake_run_range)
    result = await run_walk_forward(
        runner=runner, windows=windows, bootstrap_iter=10
    )
    # 2 windows × 2 calls each (train + test) = 4 calls
    assert len(range_calls) == 4
    assert range_calls[0] == (windows[0].train_start, windows[0].train_end)
    assert range_calls[1] == (windows[0].test_start, windows[0].test_end)
    assert range_calls[2] == (windows[1].train_start, windows[1].train_end)
    assert range_calls[3] == (windows[1].test_start, windows[1].test_end)
    assert result.n_windows == 2
    assert len(result.per_window_metrics) == 2


@pytest.mark.asyncio
async def test_run_walk_forward_handles_empty_windows():
    runner = SimpleNamespace(run_range=AsyncMock())
    result = await run_walk_forward(runner=runner, windows=[], bootstrap_iter=10)
    assert result.n_windows == 0
    assert result.median_sharpe == 0.0
