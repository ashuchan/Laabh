"""Compare backtest results against live paper-trading on the same dates.

After running quant-mode live for a week, replay those exact dates through
the backtest harness and use this module to quantify how closely the
backtest approximates reality. The headline number is the *fidelity score*:

    fidelity = 1 - mean(|daily_pnl_delta|) / mean(|daily_live_pnl|)

A score near 1 means backtest and live track each other well; a score
near or below 0 means the backtest is misleading and shouldn't be trusted.

Pure-function module. Accepts pre-loaded rows (typed via Protocol so both
``QuantTrade`` and ``BacktestTrade`` ORM rows fit). No DB.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Protocol


class _TradeLike(Protocol):
    """Common trade-row shape (live ``QuantTrade`` and ``BacktestTrade`` both fit)."""

    arm_id: str
    realized_pnl: object
    entry_at: datetime


@dataclass
class PerDateDelta:
    """Diff of one date's totals between live and backtest."""

    date: date
    live_pnl: float
    backtest_pnl: float
    live_trade_count: int
    backtest_trade_count: int

    @property
    def pnl_delta(self) -> float:
        return self.backtest_pnl - self.live_pnl

    @property
    def trade_count_delta(self) -> int:
        return self.backtest_trade_count - self.live_trade_count


@dataclass
class PerArmDelta:
    """Per-arm trade-count delta across the whole compared range."""

    arm_id: str
    live_count: int
    backtest_count: int

    @property
    def count_delta(self) -> int:
        return self.backtest_count - self.live_count


@dataclass
class TradeDiff:
    """A trade present in one ledger but not the other (or with different P&L)."""

    date: date
    arm_id: str
    side: str            # "live_only" | "backtest_only" | "both_diverge"
    live_pnl: float | None
    backtest_pnl: float | None


@dataclass
class CompareResult:
    """Aggregate comparison output."""

    per_date: list[PerDateDelta] = field(default_factory=list)
    per_arm: list[PerArmDelta] = field(default_factory=list)
    diffs: list[TradeDiff] = field(default_factory=list)

    @property
    def fidelity_score(self) -> float:
        """1 - mean(|pnl_delta|) / mean(|live_pnl|).

        Convention:
          * Returns 1.0 when there are no dates at all (vacuous match).
          * Returns 0.0 when live P&L is identically zero but backtest isn't —
            denominator is 0; we cap at 0 (worst case) rather than +inf.
        """
        if not self.per_date:
            return 1.0
        mean_abs_delta = sum(abs(d.pnl_delta) for d in self.per_date) / len(self.per_date)
        mean_abs_live = sum(abs(d.live_pnl) for d in self.per_date) / len(self.per_date)
        if mean_abs_live == 0:
            return 1.0 if mean_abs_delta == 0 else 0.0
        score = 1.0 - mean_abs_delta / mean_abs_live
        # Bound below at 0 — unbounded above is meaningless for fidelity.
        return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _coerce_float(v: object) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _trade_date(t: _TradeLike) -> date:
    return t.entry_at.date() if t.entry_at is not None else date.min


# ---------------------------------------------------------------------------
# Public diff
# ---------------------------------------------------------------------------

def compare(
    live_trades: Iterable[_TradeLike],
    backtest_trades: Iterable[_TradeLike],
) -> CompareResult:
    """Diff two trade-row collections covering the same date range.

    Both inputs should have already been filtered to the comparison window.

    Returns:
        ``CompareResult`` containing per-date deltas, per-arm deltas, and
        per-trade-level diffs (live-only, backtest-only).
    """
    live_list = list(live_trades)
    bt_list = list(backtest_trades)

    # Per-date P&L + count totals
    live_by_date: dict[date, list[_TradeLike]] = defaultdict(list)
    bt_by_date: dict[date, list[_TradeLike]] = defaultdict(list)
    for t in live_list:
        live_by_date[_trade_date(t)].append(t)
    for t in bt_list:
        bt_by_date[_trade_date(t)].append(t)

    all_dates = sorted(set(live_by_date) | set(bt_by_date))
    per_date: list[PerDateDelta] = []
    for d in all_dates:
        live_today = live_by_date.get(d, [])
        bt_today = bt_by_date.get(d, [])
        per_date.append(
            PerDateDelta(
                date=d,
                live_pnl=sum(_coerce_float(t.realized_pnl) for t in live_today),
                backtest_pnl=sum(_coerce_float(t.realized_pnl) for t in bt_today),
                live_trade_count=len(live_today),
                backtest_trade_count=len(bt_today),
            )
        )

    # Per-arm trade-count deltas across whole range
    live_arm_counts: dict[str, int] = defaultdict(int)
    bt_arm_counts: dict[str, int] = defaultdict(int)
    for t in live_list:
        live_arm_counts[t.arm_id] += 1
    for t in bt_list:
        bt_arm_counts[t.arm_id] += 1
    all_arms = sorted(set(live_arm_counts) | set(bt_arm_counts))
    per_arm = [
        PerArmDelta(
            arm_id=a,
            live_count=live_arm_counts.get(a, 0),
            backtest_count=bt_arm_counts.get(a, 0),
        )
        for a in all_arms
    ]

    # Per-trade-level diffs: a trade is "live_only" if its (date, arm_id) has
    # more live trades than backtest, and vice versa. We don't try to pair
    # individual trades — entry timestamps can drift across the two paths.
    diffs: list[TradeDiff] = []
    for d in all_dates:
        live_today = live_by_date.get(d, [])
        bt_today = bt_by_date.get(d, [])
        live_arm: dict[str, list[_TradeLike]] = defaultdict(list)
        bt_arm: dict[str, list[_TradeLike]] = defaultdict(list)
        for t in live_today:
            live_arm[t.arm_id].append(t)
        for t in bt_today:
            bt_arm[t.arm_id].append(t)
        arms_today = set(live_arm) | set(bt_arm)
        for a in sorted(arms_today):
            live_n = len(live_arm.get(a, []))
            bt_n = len(bt_arm.get(a, []))
            if live_n > bt_n:
                # live-only excess trades
                for t in live_arm[a][bt_n:]:
                    diffs.append(
                        TradeDiff(
                            date=d, arm_id=a, side="live_only",
                            live_pnl=_coerce_float(t.realized_pnl),
                            backtest_pnl=None,
                        )
                    )
            elif bt_n > live_n:
                for t in bt_arm[a][live_n:]:
                    diffs.append(
                        TradeDiff(
                            date=d, arm_id=a, side="backtest_only",
                            live_pnl=None,
                            backtest_pnl=_coerce_float(t.realized_pnl),
                        )
                    )

    return CompareResult(per_date=per_date, per_arm=per_arm, diffs=diffs)
