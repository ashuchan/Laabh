"""Tests for the backtest-vs-live comparison module."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.quant.backtest.reporting.compare_modes import (
    CompareResult,
    PerArmDelta,
    PerDateDelta,
    TradeDiff,
    compare,
)


class _T:
    def __init__(self, arm_id: str, pnl: float | None, when: datetime):
        self.arm_id = arm_id
        self.realized_pnl = Decimal(str(pnl)) if pnl is not None else None
        self.entry_at = when


def _at(d: date, hour: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Per-date deltas
# ---------------------------------------------------------------------------

def test_per_date_aggregates_pnl_and_counts():
    d = date(2026, 4, 27)
    live = [_T("A_orb", 100, _at(d)), _T("B_vwap", -50, _at(d))]
    bt = [_T("A_orb", 90, _at(d)), _T("B_vwap", -60, _at(d))]
    result = compare(live, bt)
    assert len(result.per_date) == 1
    pd = result.per_date[0]
    assert pd.date == d
    assert pd.live_pnl == 50.0       # 100 - 50
    assert pd.backtest_pnl == 30.0   # 90 - 60
    assert pd.pnl_delta == -20.0
    assert pd.live_trade_count == 2
    assert pd.backtest_trade_count == 2


def test_per_date_handles_dates_in_only_one_ledger():
    d1, d2 = date(2026, 4, 27), date(2026, 4, 28)
    live = [_T("A", 100, _at(d1))]
    bt = [_T("A", 100, _at(d2))]
    result = compare(live, bt)
    dates = {pd.date for pd in result.per_date}
    assert dates == {d1, d2}


def test_per_date_sorted_chronologically():
    d1, d2, d3 = date(2026, 4, 27), date(2026, 4, 28), date(2026, 4, 29)
    live = [_T("A", 1, _at(d3)), _T("A", 1, _at(d1))]
    bt = [_T("A", 1, _at(d2))]
    result = compare(live, bt)
    assert [pd.date for pd in result.per_date] == [d1, d2, d3]


# ---------------------------------------------------------------------------
# Per-arm deltas
# ---------------------------------------------------------------------------

def test_per_arm_aggregates_counts_across_dates():
    d1, d2 = date(2026, 4, 27), date(2026, 4, 28)
    live = [_T("A", 1, _at(d1)), _T("A", 1, _at(d2)), _T("B", 1, _at(d1))]
    bt = [_T("A", 1, _at(d1)), _T("C", 1, _at(d2))]
    result = compare(live, bt)
    by_arm = {a.arm_id: a for a in result.per_arm}
    assert by_arm["A"].live_count == 2
    assert by_arm["A"].backtest_count == 1
    assert by_arm["A"].count_delta == -1
    assert by_arm["B"].live_count == 1
    assert by_arm["B"].backtest_count == 0
    assert by_arm["C"].live_count == 0
    assert by_arm["C"].backtest_count == 1


# ---------------------------------------------------------------------------
# Trade-level diffs
# ---------------------------------------------------------------------------

def test_diffs_flag_live_only_extra_trade():
    d = date(2026, 4, 27)
    live = [_T("A", 100, _at(d)), _T("A", 50, _at(d, hour=11))]
    bt = [_T("A", 100, _at(d))]
    result = compare(live, bt)
    live_only = [x for x in result.diffs if x.side == "live_only"]
    assert len(live_only) == 1
    assert live_only[0].date == d
    assert live_only[0].arm_id == "A"
    assert live_only[0].live_pnl == 50.0


def test_diffs_flag_backtest_only_extra_trade():
    d = date(2026, 4, 27)
    live = [_T("A", 100, _at(d))]
    bt = [_T("A", 100, _at(d)), _T("A", 50, _at(d, hour=11))]
    result = compare(live, bt)
    bt_only = [x for x in result.diffs if x.side == "backtest_only"]
    assert len(bt_only) == 1
    assert bt_only[0].backtest_pnl == 50.0


def test_no_diffs_when_counts_match_per_arm_per_date():
    d = date(2026, 4, 27)
    live = [_T("A", 100, _at(d)), _T("B", 50, _at(d))]
    bt = [_T("A", 110, _at(d)), _T("B", 40, _at(d))]
    result = compare(live, bt)
    # Counts match — no presence-level diffs (P&L deltas are captured in
    # per_date totals).
    assert result.diffs == []


# ---------------------------------------------------------------------------
# Fidelity score
# ---------------------------------------------------------------------------

def test_fidelity_one_for_perfect_match():
    d = date(2026, 4, 27)
    live = [_T("A", 100, _at(d))]
    bt = [_T("A", 100, _at(d))]
    result = compare(live, bt)
    assert result.fidelity_score == 1.0


def test_fidelity_low_for_large_pnl_drift():
    d1, d2 = date(2026, 4, 27), date(2026, 4, 28)
    live = [_T("A", 100, _at(d1)), _T("A", 100, _at(d2))]
    bt = [_T("A", -100, _at(d1)), _T("A", -100, _at(d2))]
    result = compare(live, bt)
    # mean|delta| = 200, mean|live| = 100 → 1 - 200/100 = -1, clamped to 0
    assert result.fidelity_score == 0.0


def test_fidelity_one_for_empty_result():
    """Vacuous — no dates at all → fidelity is 1.0 (nothing to disagree on)."""
    result = compare([], [])
    assert result.fidelity_score == 1.0


def test_fidelity_zero_when_live_zero_pnl_but_backtest_nonzero():
    d = date(2026, 4, 27)
    live = [_T("A", 0, _at(d))]
    bt = [_T("A", 100, _at(d))]
    result = compare(live, bt)
    # mean|live| = 0 → fidelity 0.0 (worst case, can't normalize)
    assert result.fidelity_score == 0.0


def test_fidelity_one_when_both_identically_zero():
    d = date(2026, 4, 27)
    live = [_T("A", 0, _at(d))]
    bt = [_T("A", 0, _at(d))]
    result = compare(live, bt)
    assert result.fidelity_score == 1.0


def test_fidelity_score_in_unit_interval():
    """Always clamped to [0, 1]."""
    import random
    rng = random.Random(7)
    d = date(2026, 4, 27)
    live = [_T("X", rng.uniform(-1000, 1000), _at(d, hour=h))
            for h in range(10, 15)]
    bt = [_T("X", rng.uniform(-1000, 1000), _at(d, hour=h))
          for h in range(10, 15)]
    result = compare(live, bt)
    assert 0.0 <= result.fidelity_score <= 1.0


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def test_cli_parser_accepts_well_formed_args():
    from scripts.backtest_compare_to_paper import build_parser

    pid = str(uuid.uuid4())
    args = build_parser().parse_args(
        [
            "--portfolio-id", pid,
            "--start-date", "2026-04-27",
            "--end-date", "2026-05-09",
        ]
    )
    assert str(args.portfolio_id) == pid
    assert args.start_date == date(2026, 4, 27)
    assert args.end_date == date(2026, 5, 9)


def test_cli_main_rejects_inverted_dates():
    from scripts import backtest_compare_to_paper
    pid = str(uuid.uuid4())
    with pytest.raises(SystemExit):
        backtest_compare_to_paper.main(
            [
                "--portfolio-id", pid,
                "--start-date", "2026-04-30",
                "--end-date", "2026-04-27",
            ]
        )
