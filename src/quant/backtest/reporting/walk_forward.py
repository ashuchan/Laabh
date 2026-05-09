"""Walk-forward validation harness (Lopez de Prado purged k-fold style).

A walk-forward window is four contiguous date ranges:

  [---- train ----][--- purge ---][----- test -----]

The bandit posteriors warm up on ``train``, are frozen during ``purge``
(no orchestrator runs at all over those dates), then run online-updating
on ``test``. The purge gap prevents information from the train tail
leaking into the test head via posterior smoothing.

Windows slide forward by ``slide_days`` (default = ``test_days``, giving
non-overlapping test slices). Adjacent windows' *train* ranges may overlap;
that's fine — only test results are pooled.

SOLID notes:
  * SRP — this module owns *only* date-arithmetic and the per-window
    delegation. Per-day replay logic lives in ``BacktestRunner``;
    metric aggregation lives in ``metrics.py``.
  * DIP — depends on the runner abstractly via duck-typed ``run_range``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Awaitable, Callable, Iterable, Sequence

from loguru import logger

from src.quant.backtest.clock import trading_days_between
from src.quant.backtest.reporting.metrics import (
    MetricsBundle,
    compute_metrics,
)


@dataclass(frozen=True)
class WalkForwardWindow:
    """One walk-forward window — four contiguous date ranges."""

    index: int
    train_start: date
    train_end: date            # inclusive
    purge_start: date          # train_end + 1
    purge_end: date            # inclusive (= test_start - 1)
    test_start: date
    test_end: date             # inclusive

    def overlaps_test(self, other: "WalkForwardWindow") -> bool:
        return not (self.test_end < other.test_start or other.test_end < self.test_start)


@dataclass
class WalkForwardResult:
    """Aggregate across all windows."""

    windows: list[WalkForwardWindow]
    per_window_metrics: list[MetricsBundle]

    @property
    def n_windows(self) -> int:
        return len(self.windows)

    @property
    def median_sharpe(self) -> float:
        sharpes = sorted(m.sharpe for m in self.per_window_metrics)
        n = len(sharpes)
        if n == 0:
            return 0.0
        if n % 2 == 1:
            return sharpes[n // 2]
        return (sharpes[n // 2 - 1] + sharpes[n // 2]) / 2.0

    @property
    def red_flag(self) -> bool:
        """Spec §8 Task 12: 'flagged if median < 0' — strategy is suspect."""
        return self.median_sharpe < 0.0


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------

def compute_windows(
    *,
    start_date: date,
    end_date: date,
    train_days: int,
    test_days: int,
    purge_days: int,
    slide_days: int | None = None,
    holidays: Iterable[date] = (),
) -> list[WalkForwardWindow]:
    """Build the walk-forward windows for a date range.

    Args:
        start_date / end_date: Outer date range (inclusive).
        train_days / test_days / purge_days: Window-segment lengths in
            *trading days* (weekends / holidays are skipped during
            enumeration).
        slide_days: How many trading days each window's start advances.
            Default = ``test_days`` (non-overlapping test slices).
        holidays: NSE holiday set.

    Returns:
        List of ``WalkForwardWindow``. Empty if the range is too short to
        contain even one window.

    The window's ``train_start`` is always a trading day; segment ends are
    the (n-1)th trading day from the segment start, so dates returned
    are real trading days the runner can replay.
    """
    if any(d <= 0 for d in (train_days, test_days)):
        raise ValueError("train_days and test_days must be positive")
    if purge_days < 0:
        raise ValueError("purge_days must be non-negative")
    if slide_days is None:
        slide_days = test_days
    if slide_days <= 0:
        raise ValueError("slide_days must be positive")

    days = trading_days_between(start_date, end_date, holidays=holidays)
    needed = train_days + purge_days + test_days
    if len(days) < needed:
        return []

    windows: list[WalkForwardWindow] = []
    i = 0
    idx = 0
    while i + needed <= len(days):
        train_start = days[i]
        train_end = days[i + train_days - 1]
        purge_start = days[i + train_days]
        purge_end = days[i + train_days + purge_days - 1] if purge_days > 0 else train_end
        test_start = days[i + train_days + purge_days]
        test_end = days[i + train_days + purge_days + test_days - 1]
        windows.append(
            WalkForwardWindow(
                index=idx,
                train_start=train_start,
                train_end=train_end,
                purge_start=purge_start,
                purge_end=purge_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        idx += 1
        i += slide_days
    return windows


def windows_have_no_overlapping_tests(windows: Sequence[WalkForwardWindow]) -> bool:
    """True iff no two windows' test ranges overlap (acceptance §13)."""
    for i, w in enumerate(windows):
        for other in windows[i + 1:]:
            if w.overlaps_test(other):
                return False
    return True


# ---------------------------------------------------------------------------
# Walk-forward execution
# ---------------------------------------------------------------------------

# Type alias: anything with an awaitable ``run_range(start, end)`` returning a
# result whose ``.days`` attribute is a list of ``SingleDayResult``-like
# objects with ``pnl_pct`` (decimal). ``BacktestRunner`` fits.
RunnerProtocol = Callable[[date, date], Awaitable["object"]]


async def run_walk_forward(
    *,
    runner,
    windows: Sequence[WalkForwardWindow],
    bootstrap_iter: int = 1000,
) -> WalkForwardResult:
    """Replay each window's ``test`` range and compute per-window metrics.

    The runner is responsible for the bandit's warm-up; it stores the
    ``train`` posteriors via ``persistence.save_eod`` at the end of each
    train day. The orchestrator then loads them at the start of every
    test day via ``persistence.load_morning``.

    For now we replay both ``train`` and ``test`` segments (the train
    portion warms the bandit; only the test daily P&L is fed into metrics).
    The ``purge`` segment is *not* replayed — that's the embargo gap.
    """
    per_window: list[MetricsBundle] = []
    for w in windows:
        logger.info(
            f"walk_forward: window {w.index} "
            f"train=[{w.train_start}..{w.train_end}] "
            f"test=[{w.test_start}..{w.test_end}]"
        )
        # Warm-up (train + purge skipped — purge means no orchestrator runs
        # on those dates, but train must run to update bandit posteriors)
        await runner.run_range(w.train_start, w.train_end)
        # Test
        test_result = await runner.run_range(w.test_start, w.test_end)
        # Convert per-day pnl_pct → returns series for metrics
        returns = [
            float(d.pnl_pct) for d in test_result.days
            if d.pnl_pct is not None
        ]
        per_window.append(compute_metrics(returns, bootstrap_iter=bootstrap_iter))

    return WalkForwardResult(
        windows=list(windows),
        per_window_metrics=per_window,
    )
