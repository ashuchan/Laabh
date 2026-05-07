"""Quant-mode EOD report — deterministic, no LLM, Telegram-friendly.

Reads quant_trades and quant_day_state for the given portfolio + date and
formats a ≤ 4096-character Telegram message.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

from loguru import logger
from sqlalchemy import select, func

from src.db import session_scope
from src.models.quant_day_state import QuantDayState
from src.models.quant_trade import QuantTrade


async def generate_eod(
    portfolio_id: uuid.UUID,
    trading_date: date,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> str:
    """Generate and send the EOD Telegram report. Returns the formatted message."""
    msg = await _build_message(portfolio_id, trading_date)
    logger.info(f"[QUANT] EOD report generated ({len(msg)} chars)")

    # Send to Telegram (best-effort — don't let failure block day-end persistence)
    try:
        from src.services.side_effect_gateway import get_gateway
        await get_gateway().send_telegram(msg)
    except Exception as exc:
        logger.warning(f"[QUANT] EOD Telegram send failed: {exc!r}")

    return msg


async def _build_message(portfolio_id: uuid.UUID, trading_date: date) -> str:
    async with session_scope() as session:
        # --- Day state ---
        day = await session.get(QuantDayState, (portfolio_id, trading_date))

        # --- All closed trades for today ---
        q = (
            select(QuantTrade)
            .where(QuantTrade.portfolio_id == portfolio_id)
            .where(QuantTrade.entry_at >= _day_start(trading_date))
            .where(QuantTrade.status == "closed")
        )
        trades = (await session.execute(q)).scalars().all()

    # --- Aggregate stats ---
    total_trades = len(trades)
    wins = [t for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t for t in trades if (t.realized_pnl or 0) <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = 100.0 * win_count / total_trades if total_trades else 0.0

    total_pnl = sum(float(t.realized_pnl or 0) for t in trades)
    total_costs = sum(float(t.estimated_costs or 0) for t in trades)
    gross_pnl = total_pnl + total_costs

    avg_holding = (
        sum(
            _holding_minutes(t)
            for t in trades
            if t.entry_at and t.exit_at
        ) / total_trades
        if total_trades else 0
    )

    # --- Per-arm breakdown ---
    arm_pnl: dict[str, float] = defaultdict(float)
    arm_count: dict[str, int] = defaultdict(int)
    for t in trades:
        arm_pnl[t.arm_id] += float(t.realized_pnl or 0)
        arm_count[t.arm_id] += 1

    sorted_arms = sorted(arm_pnl.items(), key=lambda x: x[1], reverse=True)
    top3 = sorted_arms[:3]
    bottom3 = sorted_arms[-3:] if len(sorted_arms) >= 3 else []

    # --- NAV ---
    starting_nav = float(day.starting_nav) if day else 0.0
    final_nav = float(day.final_nav) if (day and day.final_nav) else starting_nav + total_pnl
    pnl_pct = (final_nav - starting_nav) / starting_nav * 100 if starting_nav else 0.0

    lockin_fired = "Yes" if (day and day.lockin_fired_at) else "No"
    kill_fired = "Yes" if (day and day.kill_switch_fired_at) else "No"

    # --- Format ---
    date_str = trading_date.strftime("%d %b %Y")
    lines = [
        f"📊 [QUANT] EOD Report — {date_str}",
        f"NAV: ₹{final_nav:,.0f} ({pnl_pct:+.2f}%)",
        f"Trades: {total_trades} ({win_count} wins, {loss_count} losses, {win_rate:.1f}% win rate)",
        f"Lock-in fired: {lockin_fired}",
        f"Kill switch: {kill_fired}",
    ]

    if top3:
        lines.append("")
        lines.append("Top arms by P&L:")
        for i, (arm_id, pnl) in enumerate(top3, 1):
            cnt = arm_count[arm_id]
            sign = "+" if pnl >= 0 else ""
            lines.append(f"  {i}. {arm_id}  {sign}₹{abs(pnl):,.0f}  ({cnt} trades)")

    if bottom3 and len(sorted_arms) > 3:
        lines.append("")
        lines.append("Bottom arms by P&L:")
        for i, (arm_id, pnl) in enumerate(bottom3, 1):
            cnt = arm_count[arm_id]
            sign = "+" if pnl >= 0 else "-"
            lines.append(f"  {i}. {arm_id}  {sign}₹{abs(pnl):,.0f}  ({cnt} trades)")

    lines.append("")
    cost_pct = 100.0 * total_costs / gross_pnl if gross_pnl else 0.0
    lines.append(f"Total costs: ₹{total_costs:,.0f} ({cost_pct:.1f}% of gross)")
    lines.append(f"Avg holding period: {avg_holding:.0f} min")

    msg = "\n".join(lines)

    # Telegram hard limit is 4096 chars; truncate with notice if needed.
    if len(msg) > 4000:
        msg = msg[:3990] + "\n…(truncated)"

    return msg


def _holding_minutes(trade: QuantTrade) -> float:
    if not trade.exit_at or not trade.entry_at:
        return 0.0
    entry = trade.entry_at.replace(tzinfo=timezone.utc) if trade.entry_at.tzinfo is None else trade.entry_at
    exit_ = trade.exit_at.replace(tzinfo=timezone.utc) if trade.exit_at.tzinfo is None else trade.exit_at
    return (exit_ - entry).total_seconds() / 60.0


def _day_start(trading_date: date) -> datetime:
    from datetime import datetime, timezone
    return datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=timezone.utc)
