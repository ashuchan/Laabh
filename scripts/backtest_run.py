"""Top-level CLI for running the quant-mode backtest harness.

Usage:
    python -m scripts.backtest_run \\
        --start-date 2025-10-01 \\
        --end-date 2025-12-31 \\
        --portfolio-id <uuid> \\
        --seed 42

The runner is implemented in ``src.quant.backtest.runner.BacktestRunner``.
This script is just an argparse + asyncio harness around it so the runner
itself stays unit-testable without subprocess spawn.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date, datetime

from loguru import logger

from src.quant.backtest.runner import BacktestRangeResult, BacktestRunner


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_uuid(s: str) -> uuid.UUID:
    try:
        return uuid.UUID(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Not a valid UUID: {s!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backtest_run",
        description="Replay a date range through the quant orchestrator with backtest I/O.",
    )
    p.add_argument("--start-date", type=_parse_date, required=True, help="YYYY-MM-DD")
    p.add_argument("--end-date", type=_parse_date, required=True, help="YYYY-MM-DD")
    p.add_argument(
        "--portfolio-id", type=_parse_uuid, required=True, help="Portfolio UUID"
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Reproducibility seed (default 42). Same seed → bit-identical results.",
    )
    p.add_argument(
        "--smile-method",
        choices=["flat", "linear", "sabr"],
        default=None,
        help="Override the IV smile method (defaults to settings).",
    )
    p.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help="Override RBI repo rate as a decimal (e.g. 0.065). Defaults to lookup.",
    )
    return p


def _print_summary(result: BacktestRangeResult) -> None:
    print()
    print("=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)
    print(f"Portfolio:    {result.portfolio_id}")
    print(f"Range:        {result.start_date} → {result.end_date}")
    print(f"Trading days: {result.n_days}  (failed: {result.n_failed})")
    print(f"Trades:       {result.total_trade_count}")
    print(f"Cumulative P&L: {result.cumulative_pnl_pct:+.4%}")
    print()
    if result.days:
        print(f"{'Date':<12} {'Start NAV':>12} {'Final NAV':>12} {'P&L %':>8} "
              f"{'Trades':>7} {'Status':>8}")
        for d in result.days:
            status = "FAILED" if d.failed else "ok"
            final = f"{d.final_nav:.2f}" if d.final_nav is not None else "—"
            pnl = f"{d.pnl_pct:+.4%}" if d.pnl_pct is not None else "—"
            tc = d.trade_count if d.trade_count is not None else "—"
            print(
                f"{d.backtest_date!s:<12} {d.starting_nav:>12.2f} {final:>12} "
                f"{pnl:>8} {tc!s:>7} {status:>8}"
            )


async def main_async(args: argparse.Namespace) -> int:
    runner = BacktestRunner(
        portfolio_id=args.portfolio_id,
        seed=args.seed,
        smile_method=args.smile_method,
        risk_free_rate=args.risk_free_rate,
    )
    result = await runner.run_range(args.start_date, args.end_date)
    _print_summary(result)
    return 0 if result.n_failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
