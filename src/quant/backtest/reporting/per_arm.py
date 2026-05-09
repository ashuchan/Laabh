"""Per-arm performance breakdown.

Groups trade rows by ``arm_id`` and computes the same metrics suite as the
top-level report does for the whole portfolio. Pure functions — accepts
already-loaded rows, no DB.

A "trade row" is a duck-typed object exposing:
  * ``arm_id: str``
  * ``realized_pnl: Decimal | float | None``
  * ``entry_at: datetime``
  * ``exit_at: datetime | None``

Both ``BacktestTrade`` and ``QuantTrade`` ORM rows fit; tests use simple
dataclasses for fixtures.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol


class _TradeLike(Protocol):
    arm_id: str
    realized_pnl: object
    entry_at: datetime
    exit_at: datetime | None


@dataclass
class ArmStats:
    """Per-arm summary."""

    arm_id: str
    trade_count: int
    pnl_total: float
    win_rate: float
    avg_holding_minutes: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe: float


def _coerce_float(v: object) -> float | None:
    """Best-effort cast for Decimal / float / None / numeric str."""
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _holding_minutes(trade: _TradeLike) -> float | None:
    if trade.exit_at is None or trade.entry_at is None:
        return None
    return (trade.exit_at - trade.entry_at).total_seconds() / 60.0


def per_arm_stats(trades: Iterable[_TradeLike]) -> list[ArmStats]:
    """Compute per-arm statistics from a flat list of trade rows.

    Trades with ``realized_pnl is None`` (still open) are excluded — they
    have no P&L to attribute. Returns one ``ArmStats`` per arm seen,
    sorted by total P&L descending so the report's leaderboard reads
    naturally.
    """
    grouped: dict[str, list[_TradeLike]] = defaultdict(list)
    for t in trades:
        if t.realized_pnl is None:
            continue
        grouped[t.arm_id].append(t)

    out: list[ArmStats] = []
    for arm_id, arm_trades in grouped.items():
        pnls = [
            v for v in (_coerce_float(t.realized_pnl) for t in arm_trades) if v is not None
        ]
        if not pnls:
            continue
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_wins = sum(wins)
        gross_losses = -sum(losses)
        pf = (
            float("inf")
            if gross_losses == 0 and gross_wins > 0
            else (gross_wins / gross_losses if gross_losses > 0 else 0.0)
        )
        holding = [h for h in (_holding_minutes(t) for t in arm_trades) if h is not None]
        # Sharpe at the per-arm level uses per-trade P&L as the unit
        # (rather than daily returns) — the spec calls for "Sharpe" per arm
        # and the trade-level interpretation is standard.
        from src.quant.backtest.reporting.metrics import sharpe as _sharpe
        sr = _sharpe(pnls, periods_per_year=1)  # trade-unit Sharpe — no annualisation
        out.append(
            ArmStats(
                arm_id=arm_id,
                trade_count=len(arm_trades),
                pnl_total=sum(pnls),
                win_rate=len(wins) / len(pnls),
                avg_holding_minutes=sum(holding) / len(holding) if holding else 0.0,
                avg_win=sum(wins) / len(wins) if wins else 0.0,
                avg_loss=sum(losses) / len(losses) if losses else 0.0,
                profit_factor=pf,
                sharpe=sr,
            )
        )
    out.sort(key=lambda s: s.pnl_total, reverse=True)
    return out
