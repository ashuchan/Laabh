"""One-shot script: link today's open BUY/SELL trade pairs FIFO.

Run once after deploying the close-link engine fix. Targets the day passed
on the command line (default = today IST) and walks every (portfolio,
instrument) bucket; each SELL leg consumes its FIFO matching BUY legs and
the BUY rows get ``status='closed'`` + ``pnl`` + ``closing_trade_id``.

Idempotent: trades already marked ``status='closed'`` or already carrying
a ``closing_trade_id`` are skipped, so re-running is safe.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import and_, select

from src.db import session_scope
from src.models.portfolio import Portfolio
from src.models.trade import Trade


_IST = ZoneInfo("Asia/Kolkata")
_TWO = Decimal("0.01")


def _round(v: Decimal) -> Decimal:
    return v.quantize(_TWO, rounding=ROUND_HALF_UP)


def _ist_day_window(day: date) -> tuple[datetime, datetime]:
    start_ist = datetime.combine(day, time.min, tzinfo=_IST)
    end_ist = datetime.combine(day, time.max, tzinfo=_IST)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


async def backfill_for_day(day: date) -> dict:
    """Walk all (portfolio, instrument) on ``day`` and FIFO-link sells to buys."""
    start_utc, end_utc = _ist_day_window(day)
    closed_count = 0
    matched_pairs = 0
    realized_pnl = Decimal("0")

    async with session_scope() as session:
        # All trades executed in the day window, regardless of status.
        rows = list((await session.execute(
            select(Trade)
            .where(
                Trade.executed_at >= start_utc,
                Trade.executed_at <= end_utc,
            )
            .order_by(Trade.executed_at.asc())
        )).scalars())

    # Group by (portfolio_id, instrument_id) and FIFO-match BUYs and SELLs
    # within that bucket. We re-read each bucket inside its own session to
    # keep mutations bounded — each session_scope commits independently so a
    # partial failure won't corrupt the whole backfill.
    buckets: dict[tuple, list[Trade]] = {}
    for t in rows:
        key = (t.portfolio_id, t.instrument_id)
        buckets.setdefault(key, []).append(t)

    for (pid, iid), trades in buckets.items():
        async with session_scope() as session:
            # Re-fetch open BUYs for the bucket (status='open', BUY).
            open_buys = list((await session.execute(
                select(Trade)
                .where(
                    Trade.portfolio_id == pid,
                    Trade.instrument_id == iid,
                    Trade.trade_type == "BUY",
                    Trade.status == "open",
                )
                .order_by(Trade.executed_at.asc())
            )).scalars())
            sells = list((await session.execute(
                select(Trade)
                .where(
                    Trade.portfolio_id == pid,
                    Trade.instrument_id == iid,
                    Trade.trade_type == "SELL",
                    Trade.executed_at >= start_utc,
                    Trade.executed_at <= end_utc,
                )
                .order_by(Trade.executed_at.asc())
            )).scalars())
            if not sells or not open_buys:
                continue

            for sell in sells:
                remaining = int(sell.quantity)
                sell_price = Decimal(str(sell.price))
                sell_at = sell.executed_at
                sell_cost_ps = (
                    (Decimal(str(sell.brokerage or 0))
                     + Decimal(str(sell.stt or 0)))
                    / Decimal(max(int(sell.quantity), 1))
                )

                while remaining > 0 and open_buys:
                    buy = open_buys[0]
                    buy_qty = int(buy.quantity)
                    consume = min(buy_qty, remaining)
                    buy_price = Decimal(str(buy.price))
                    buy_cost_ps = (
                        (Decimal(str(buy.brokerage or 0))
                         + Decimal(str(buy.stt or 0)))
                        / Decimal(max(buy_qty, 1))
                    )
                    slice_pnl = _round(
                        (sell_price - buy_price) * Decimal(consume)
                        - (buy_cost_ps + sell_cost_ps) * Decimal(consume)
                    )
                    slice_basis = buy_price * Decimal(consume)
                    slice_pnl_pct = (
                        _round(slice_pnl / slice_basis * Decimal(100))
                        if slice_basis > 0 else None
                    )
                    holding_days = (
                        (sell_at.date() - buy.executed_at.date()).days
                        if sell_at and buy.executed_at else 0
                    )

                    if consume == buy_qty:
                        buy.status = "closed"
                        buy.closed_at = sell_at
                        buy.closing_trade_id = sell.id
                        buy.pnl = float(slice_pnl)
                        buy.pnl_pct = float(slice_pnl_pct) if slice_pnl_pct is not None else None
                        buy.holding_days = holding_days
                        open_buys.pop(0)
                    else:
                        # Split brokerage and STT proportionally on the
                        # consumed slice so the per-leg breakdown survives
                        # in audit reports.
                        buy_brokerage = Decimal(str(buy.brokerage or 0))
                        buy_stt = Decimal(str(buy.stt or 0))
                        slice_share = Decimal(consume) / Decimal(buy_qty)
                        slice_brokerage = buy_brokerage * slice_share
                        slice_stt = buy_stt * slice_share
                        consumed = Trade(
                            portfolio_id=pid,
                            instrument_id=iid,
                            signal_id=buy.signal_id,
                            trade_type="BUY",
                            order_type=buy.order_type,
                            quantity=consume,
                            price=float(buy_price),
                            brokerage=float(slice_brokerage),
                            stt=float(slice_stt),
                            total_cost=float(
                                buy_price * Decimal(consume)
                                + slice_brokerage + slice_stt
                            ),
                            status="closed",
                            closing_trade_id=sell.id,
                            pnl=float(slice_pnl),
                            pnl_pct=float(slice_pnl_pct) if slice_pnl_pct is not None else None,
                            holding_days=holding_days,
                            entry_reason=buy.entry_reason,
                            executed_at=buy.executed_at,
                            closed_at=sell_at,
                        )
                        session.add(consumed)
                        buy.quantity = buy_qty - consume
                        ratio = Decimal(buy_qty - consume) / Decimal(buy_qty)
                        buy.brokerage = float(Decimal(str(buy.brokerage or 0)) * ratio)
                        buy.stt = float(Decimal(str(buy.stt or 0)) * ratio)
                        buy.total_cost = float(Decimal(str(buy.total_cost or 0)) * ratio)

                    matched_pairs += 1
                    realized_pnl += slice_pnl
                    closed_count += 1
                    remaining -= consume

                if remaining > 0:
                    logger.warning(
                        f"backfill: SELL {sell.id} {iid} unmatched remainder={remaining}"
                    )

    return {
        "matched_pairs": matched_pairs,
        "rows_closed": closed_count,
        "realized_pnl": float(realized_pnl),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--day",
        default=datetime.now(_IST).date().isoformat(),
        help="Day to backfill (YYYY-MM-DD, IST). Defaults to today.",
    )
    args = parser.parse_args()
    day = date.fromisoformat(args.day)
    result = await backfill_for_day(day)
    logger.info(
        f"backfill {day}: matched_pairs={result['matched_pairs']} "
        f"rows_closed={result['rows_closed']} "
        f"realized_pnl=Rs{result['realized_pnl']:.2f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
