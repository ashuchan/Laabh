"""Compare backtest results to live paper-trading on the same dates.

Reads ``quant_trades`` (live ledger) and ``backtest_trades`` (filtered to
runs in the requested portfolio + date range) and prints a per-date /
per-arm delta plus a fidelity score.

Usage:
    python -m scripts.backtest_compare_to_paper \\
        --portfolio-id <uuid> \\
        --start-date 2026-04-27 \\
        --end-date 2026-05-09
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date, datetime, time, timezone

import pytz
from sqlalchemy import select

from src.db import session_scope
from src.models.backtest_run import BacktestRun
from src.models.backtest_trade import BacktestTrade
from src.models.quant_trade import QuantTrade
from src.quant.backtest.reporting.compare_modes import CompareResult, compare


_IST = pytz.timezone("Asia/Kolkata")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_uuid(s: str) -> uuid.UUID:
    try:
        return uuid.UUID(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Not a valid UUID: {s!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backtest_compare_to_paper",
        description="Compare quant_trades (live) vs backtest_trades over a date range.",
    )
    p.add_argument("--portfolio-id", type=_parse_uuid, required=True)
    p.add_argument("--start-date", type=_parse_date, required=True)
    p.add_argument("--end-date", type=_parse_date, required=True)
    return p


async def _load_live_trades(
    portfolio_id: uuid.UUID, start_date: date, end_date: date
) -> list[QuantTrade]:
    start_dt = _IST.localize(datetime.combine(start_date, time(0, 0))).astimezone(timezone.utc)
    end_dt = _IST.localize(datetime.combine(end_date, time(23, 59, 59))).astimezone(timezone.utc)
    async with session_scope() as session:
        q = (
            select(QuantTrade)
            .where(QuantTrade.portfolio_id == portfolio_id)
            .where(QuantTrade.entry_at >= start_dt)
            .where(QuantTrade.entry_at <= end_dt)
            .order_by(QuantTrade.entry_at)
        )
        return list((await session.execute(q)).scalars())


async def _load_backtest_trades(
    portfolio_id: uuid.UUID, start_date: date, end_date: date
) -> list[BacktestTrade]:
    """Pull all backtest trades whose run was for ``portfolio_id`` in range."""
    async with session_scope() as session:
        runs_q = (
            select(BacktestRun.id)
            .where(BacktestRun.portfolio_id == portfolio_id)
            .where(BacktestRun.backtest_date >= start_date)
            .where(BacktestRun.backtest_date <= end_date)
        )
        run_ids = [r[0] for r in (await session.execute(runs_q)).all()]
        if not run_ids:
            return []
        q = (
            select(BacktestTrade)
            .where(BacktestTrade.backtest_run_id.in_(run_ids))
            .order_by(BacktestTrade.entry_at)
        )
        return list((await session.execute(q)).scalars())


def _print_report(result: CompareResult) -> None:
    print("=" * 60)
    print("BACKTEST ↔ LIVE COMPARISON")
    print("=" * 60)
    print(f"Fidelity score: {result.fidelity_score:.4f}  (1.0 = perfect, 0.0 = worst)")
    print()
    if result.per_date:
        print(f"{'Date':<12} {'Live P&L':>10} {'BT P&L':>10} {'Δ P&L':>10} "
              f"{'Live #':>7} {'BT #':>5} {'Δ #':>5}")
        for d in result.per_date:
            print(
                f"{d.date!s:<12} {d.live_pnl:>10.2f} {d.backtest_pnl:>10.2f} "
                f"{d.pnl_delta:>+10.2f} {d.live_trade_count:>7} "
                f"{d.backtest_trade_count:>5} {d.trade_count_delta:>+5}"
            )
    print()
    if result.per_arm:
        print("Per-arm trade-count deltas (only arms with non-zero delta):")
        for a in result.per_arm:
            if a.count_delta == 0:
                continue
            print(
                f"  {a.arm_id:<30} live={a.live_count:>3} bt={a.backtest_count:>3} "
                f"Δ={a.count_delta:>+3}"
            )
    print()
    if result.diffs:
        print(f"Trade-level diffs ({len(result.diffs)} total — first 10):")
        for d in result.diffs[:10]:
            live = f"{d.live_pnl:.2f}" if d.live_pnl is not None else "—"
            bt = f"{d.backtest_pnl:.2f}" if d.backtest_pnl is not None else "—"
            print(
                f"  {d.date!s} {d.arm_id:<25} {d.side:<14} live={live} bt={bt}"
            )


async def main_async(args: argparse.Namespace) -> int:
    live = await _load_live_trades(args.portfolio_id, args.start_date, args.end_date)
    bt = await _load_backtest_trades(args.portfolio_id, args.start_date, args.end_date)
    result = compare(live, bt)
    _print_report(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
