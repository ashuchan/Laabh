"""Tests for IV history builder — pure computation helpers only."""
from __future__ import annotations

import pytest

from src.fno.iv_history_builder import (
    compute_iv_percentile,
    compute_iv_rank,
    select_atm_iv,
)


# ---------------------------------------------------------------------------
# compute_iv_rank
# ---------------------------------------------------------------------------

def test_iv_rank_middle() -> None:
    history = [10.0, 20.0, 30.0]
    assert compute_iv_rank(20.0, history) == 50.0


def test_iv_rank_at_max() -> None:
    history = [10.0, 20.0, 30.0]
    assert compute_iv_rank(30.0, history) == 100.0


def test_iv_rank_at_min() -> None:
    history = [10.0, 20.0, 30.0]
    assert compute_iv_rank(10.0, history) == 0.0


def test_iv_rank_above_history_max() -> None:
    history = [10.0, 20.0]
    rank = compute_iv_rank(25.0, history)
    assert rank == 150.0  # can exceed 100 when current > max


def test_iv_rank_empty_history_returns_none() -> None:
    assert compute_iv_rank(20.0, []) is None


def test_iv_rank_flat_history_returns_50() -> None:
    assert compute_iv_rank(15.0, [15.0, 15.0, 15.0]) == 50.0


# ---------------------------------------------------------------------------
# compute_iv_percentile
# ---------------------------------------------------------------------------

def test_iv_percentile_all_below() -> None:
    history = [10.0, 12.0, 14.0]
    assert compute_iv_percentile(20.0, history) == 100.0


def test_iv_percentile_none_below() -> None:
    history = [20.0, 25.0, 30.0]
    assert compute_iv_percentile(15.0, history) == 0.0


def test_iv_percentile_half_below() -> None:
    history = [10.0, 15.0, 20.0, 25.0]
    # current=18: below are 10, 15 → 2/4 = 50%
    assert compute_iv_percentile(18.0, history) == 50.0


def test_iv_percentile_empty_returns_none() -> None:
    assert compute_iv_percentile(20.0, []) is None


# ---------------------------------------------------------------------------
# select_atm_iv
# ---------------------------------------------------------------------------

def _rows(*args: tuple[str, float, float]) -> list[tuple[str, float, float]]:
    return list(args)


def test_select_atm_iv_picks_closest_strike() -> None:
    rows = _rows(
        ("CE", 900.0, 0.20),
        ("CE", 1000.0, 0.18),
        ("CE", 1100.0, 0.22),
        ("PE", 900.0, 0.19),
        ("PE", 1000.0, 0.17),
        ("PE", 1100.0, 0.21),
    )
    iv = select_atm_iv(rows, underlying_price=1005.0)
    # Closest strike to 1005 is 1000; average CE=0.18 and PE=0.17 = 0.175
    assert iv is not None
    assert abs(iv - 0.175) < 0.001


def test_select_atm_iv_single_side() -> None:
    rows = _rows(("CE", 1000.0, 0.20))
    iv = select_atm_iv(rows, underlying_price=1000.0)
    assert iv == 0.20


def test_select_atm_iv_empty_returns_none() -> None:
    assert select_atm_iv([], underlying_price=1000.0) is None


def test_select_atm_iv_none_iv_skipped() -> None:
    rows: list[tuple[str, float, float | None]] = [
        ("CE", 1000.0, None),
        ("PE", 1000.0, 0.20),
    ]
    iv = select_atm_iv(rows, underlying_price=1000.0)  # type: ignore[arg-type]
    assert iv == 0.20
