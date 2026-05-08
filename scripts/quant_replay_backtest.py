#!/usr/bin/env python3
"""Quant replay backtest — replays options_chain snapshots through the orchestrator.

Usage:
    python scripts/quant_replay_backtest.py --date 2026-04-28
    python scripts/quant_replay_backtest.py --date 2026-04-28 --portfolio-id <uuid>

Outputs per-arm performance, trade-level audit, and hypothetical NAV curve.

The replay uses as_of timestamps to replay feature_store.get() from historical
chain snapshots without touching live data. All writes go to the normal DB but
trades are tagged with a dryrun_run_id so they can be distinguished from live
quant trades.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date, datetime, timezone

sys.path.insert(0, ".")


async def _get_or_create_portfolio(portfolio_id_str: str | None) -> uuid.UUID:
    from src.db import session_scope
    from src.models.portfolio import Portfolio
    from sqlalchemy import select

    async with session_scope() as session:
        if portfolio_id_str:
            pid = uuid.UUID(portfolio_id_str)
            row = await session.get(Portfolio, pid)
            if row is None:
                raise ValueError(f"Portfolio {pid} not found")
            return pid
        row = (await session.execute(select(Portfolio).limit(1))).scalar_one_or_none()
        if row is None:
            raise ValueError("No portfolio found. Run scripts/seed_runday_essentials.py first.")
        return row.id


async def run_backtest(replay_date: date, portfolio_id_str: str | None) -> None:
    from src.db import session_scope
    from src.quant.orchestrator import run_loop

    portfolio_id = await _get_or_create_portfolio(portfolio_id_str)
    dryrun_id = uuid.uuid4()

    print(f"=== Quant Replay Backtest ===")
    print(f"Date       : {replay_date}")
    print(f"Portfolio  : {portfolio_id}")
    print(f"Dryrun ID  : {dryrun_id}")
    print()

    # The orchestrator's as_of parameter makes it use historical chain data
    # (feature_store.get() returns data captured on replay_date).
    # The loop terminates at hard-exit time because it compares against
    # the replayed timestamps (no real sleep in replay mode).
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    replay_as_of = ist.localize(
        datetime.combine(replay_date, datetime.min.time().replace(hour=9, minute=15))
    ).astimezone(timezone.utc)

    await run_loop(portfolio_id, as_of=replay_as_of, dryrun_run_id=dryrun_id)

    # --- Print summary ---
    from src.db import session_scope
    from src.models.quant_trade import QuantTrade
    from sqlalchemy import select

    async with session_scope() as session:
        q = (
            select(QuantTrade)
            .where(QuantTrade.portfolio_id == portfolio_id)
            .order_by(QuantTrade.entry_at)
        )
        trades = (await session.execute(q)).scalars().all()

    print(f"\n{'='*60}")
    print(f"TRADE AUDIT ({len(trades)} trades)")
    print(f"{'='*60}")
    print(f"{'ARM':<30} {'LOTS':>4} {'ENTRY':>8} {'EXIT':>8} {'P&L':>10} {'REASON':<15}")
    print("-" * 80)

    nav_curve = []
    running_pnl = 0.0
    for t in trades:
        pnl = float(t.realized_pnl or 0)
        running_pnl += pnl
        nav_curve.append(running_pnl)
        entry_p = float(t.entry_premium_net)
        exit_p = float(t.exit_premium_net or 0)
        print(
            f"{t.arm_id:<30} {t.lots:>4} {entry_p:>8.1f} {exit_p:>8.1f} "
            f"{pnl:>+10.0f} {(t.exit_reason or ''):.<15}"
        )

    print("-" * 80)
    total_pnl = sum(float(t.realized_pnl or 0) for t in trades)
    wins = sum(1 for t in trades if (t.realized_pnl or 0) > 0)
    print(f"Total P&L: ₹{total_pnl:+,.0f}  |  Win rate: {wins}/{len(trades)}")

    if nav_curve:
        max_nav = max(nav_curve)
        min_nav = min(nav_curve)
        peak_idx = nav_curve.index(max_nav)
        trough_after_peak = min(nav_curve[peak_idx:]) if peak_idx < len(nav_curve) - 1 else 0
        max_dd = min_nav - max_nav
        print(f"Max drawdown: ₹{max_dd:,.0f}")

    print(f"\nDryrun ID for audit: {dryrun_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Quant mode replay backtest")
    parser.add_argument("--date", required=True, help="Replay date YYYY-MM-DD")
    parser.add_argument("--portfolio-id", default=None, help="Portfolio UUID (optional)")
    args = parser.parse_args()

    try:
        replay_date = date.fromisoformat(args.date)
    except ValueError as e:
        print(f"Invalid date: {e}")
        sys.exit(1)

    asyncio.run(run_backtest(replay_date, args.portfolio_id))


if __name__ == "__main__":
    main()
