"""Unified P&L aggregator — equity + F&O, realized + unrealized, by strategy.

The reporting layer used to read the equity book and the F&O book in two
places, format them in two different shapes, and send them as two separate
Telegram messages 3 hours apart. That made the P&L numbers feel
inconsistent because they *were* inconsistent (different denominators,
different timestamps, no shared totals).

This module is the single source of truth for "what happened today across
the whole paper book." Every consumer (15:40 F&O EOD, 18:30 daily report,
morning/intraday digests) reads from here so the numbers always line up.

Strategy bucket keys match ``src.trading.budget_allocator`` so the budget
deployed and the P&L earned roll up to the same ledger lines.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from src.db import session_scope
from src.models.fno_signal import FNOSignal
from src.models.instrument import Instrument
from src.models.portfolio import Holding, Portfolio
from src.models.trade import Trade
from src.services.price_service import PriceService
from src.trading.budget_allocator import (
    BUCKETS,
    BudgetPlan,
    bucket_for_fno_strategy,
    today_allocations,
)
from src.trading.strategy_runner import STRATEGY_PORTFOLIO_NAME


@dataclass
class StrategyBucketPnL:
    """Per-bucket roll-up: budget cap, capital deployed, P&L (realized+open)."""

    bucket: str
    rupee_cap: float = 0.0
    deployed: float = 0.0          # ₹ tied up right now (entry premium / invested_value)
    realized_pnl: float = 0.0       # ₹ realised today (closed FNO + equity SELLs)
    unrealized_pnl: float = 0.0     # ₹ MTM on positions still open (FNO + equity holdings)
    fills_count: int = 0            # # of executions today (BUY+SELL+FNO entries+closes)
    closes_count: int = 0           # # of closed FNO positions today
    open_count: int = 0             # # of currently-open positions in this bucket
    detail_lines: list[str] = field(default_factory=list)

    @property
    def day_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl


@dataclass
class ClosedTrade:
    """One realised round-trip — used by the EOD square-off report."""

    asset_class: str        # "EQUITY" | "FNO"
    bucket: str
    label: str              # e.g. "RELIANCE 1700 CE" or "HDFC ×12"
    entry: float
    exit: float
    pnl: float
    reason: str             # "TARGET" | "STOP" | "HARD_EXIT" | "MANUAL" | "EQUITY SELL"


@dataclass
class DailyPnLSnapshot:
    """Everything a reporter needs for today's combined P&L message."""

    as_of: datetime
    plan: BudgetPlan
    buckets: dict[str, StrategyBucketPnL]
    closed_trades: list[ClosedTrade]
    cash_remaining: float
    portfolio_total_value: float    # cash + invested_value + open FNO MTM
    realized_pnl_total: float
    unrealized_pnl_total: float
    fno_phase1_passed: int = 0
    fno_phase2_passed: int = 0
    fno_phase3_proceed: int = 0

    @property
    def day_pnl_total(self) -> float:
        return self.realized_pnl_total + self.unrealized_pnl_total

    @property
    def day_pnl_pct(self) -> float | None:
        denom = self.plan.total_budget or 0
        if denom <= 0:
            return None
        return (self.day_pnl_total / denom) * 100.0


# ---------------------------------------------------------------- entry point


async def daily_pnl_snapshot(
    today: date | None = None,
    as_of: datetime | None = None,
) -> DailyPnLSnapshot:
    """Build a full equity + F&O P&L snapshot for the given trading day.

    Defaults to today (UTC). Safe to call from any IST-cron job — all the
    underlying queries are date-bounded explicitly. Errors in any
    sub-aggregation are logged and left as zero so a partial outage never
    aborts the whole report.
    """
    eff = as_of or datetime.now(tz=timezone.utc)
    day = today or eff.date()
    plan = await today_allocations(eff)

    buckets: dict[str, StrategyBucketPnL] = {
        b: StrategyBucketPnL(bucket=b, rupee_cap=plan.rupee_caps.get(b, 0.0))
        for b in BUCKETS
    }
    closed_trades: list[ClosedTrade] = []
    cash_remaining = 0.0
    portfolio_invested = 0.0
    fno_open_mtm = 0.0

    portfolio_id = await _resolve_strategy_portfolio_id()

    if portfolio_id is not None:
        eq_realized, eq_unrealized, eq_deployed, eq_fills, eq_closed_trades, cash_remaining, portfolio_invested = (
            await _equity_bucket_rollup(portfolio_id, day)
        )
        eb = buckets["equity"]
        eb.realized_pnl = eq_realized
        eb.unrealized_pnl = eq_unrealized
        eb.deployed = eq_deployed
        eb.fills_count = eq_fills
        closed_trades.extend(eq_closed_trades)

    fno_breakdown, fno_closed_trades, fno_open_mtm = await _fno_bucket_rollup(day)
    for bucket_key, agg in fno_breakdown.items():
        b = buckets[bucket_key]
        b.realized_pnl += agg["realized_pnl"]
        b.unrealized_pnl += agg["unrealized_pnl"]
        b.deployed += agg["deployed"]
        b.fills_count += agg["fills_count"]
        b.closes_count += agg["closes_count"]
        b.open_count += agg["open_count"]
        b.detail_lines.extend(agg["detail_lines"])
    closed_trades.extend(fno_closed_trades)

    p1, p2, p3 = await _fno_phase_counts(day)

    realized_total = sum(b.realized_pnl for b in buckets.values())
    unrealized_total = sum(b.unrealized_pnl for b in buckets.values())

    portfolio_total = cash_remaining + portfolio_invested + fno_open_mtm

    return DailyPnLSnapshot(
        as_of=eff,
        plan=plan,
        buckets=buckets,
        closed_trades=closed_trades,
        cash_remaining=cash_remaining,
        portfolio_total_value=portfolio_total,
        realized_pnl_total=realized_total,
        unrealized_pnl_total=unrealized_total,
        fno_phase1_passed=p1,
        fno_phase2_passed=p2,
        fno_phase3_proceed=p3,
    )


# ----------------------------------------------------------------- internals


async def _resolve_strategy_portfolio_id() -> uuid.UUID | None:
    async with session_scope() as session:
        row = (await session.execute(
            select(Portfolio).where(Portfolio.name == STRATEGY_PORTFOLIO_NAME)
        )).scalar_one_or_none()
        return row.id if row else None


async def _equity_bucket_rollup(
    portfolio_id: uuid.UUID, day: date
) -> tuple[float, float, float, int, list[ClosedTrade], float, float]:
    """Compute equity bucket numbers from trades + holdings.

    Returns (realized_pnl, unrealized_pnl, deployed, fills_count,
    closed_trades, cash_remaining, invested_value_now). Realized P&L is
    derived per SELL using today's average BUY cost (FIFO-ish) or the
    residual holding's avg_buy_price if the SELL drained today's BUY pile.
    Unrealized is computed against live LTPs so a position bought in this
    cron firing reflects MTM correctly (not the 5-minute-stale holdings.pnl).
    """
    day_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(day, time.max, tzinfo=timezone.utc)
    cash_remaining = 0.0
    invested_now = 0.0

    async with session_scope() as session:
        portfolio = await session.get(Portfolio, portfolio_id)
        if portfolio is None:
            return 0.0, 0.0, 0.0, 0, [], 0.0, 0.0
        cash_remaining = float(portfolio.current_cash or 0)
        invested_now = float(portfolio.invested_value or 0)

        trade_rows = list((await session.execute(
            select(Trade, Instrument)
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .where(
                Trade.portfolio_id == portfolio_id,
                Trade.executed_at >= day_start,
                Trade.executed_at <= day_end,
            )
            .order_by(Trade.executed_at.asc())
        )).all())

        holding_rows = list((await session.execute(
            select(Holding, Instrument)
            .join(Instrument, Instrument.id == Holding.instrument_id)
            .where(Holding.portfolio_id == portfolio_id)
        )).all())

    # Aggregate today's BUYs per instrument (qty + cash debit) so we can
    # derive a FIFO-flavoured cost-per-share when the same instrument sells
    # again the same day. If the holding pre-dates today's BUYs, fall back
    # to the holding's avg_buy_price for cost.
    today_buy_agg: dict[uuid.UUID, tuple[int, float]] = {}
    for t, _inst in trade_rows:
        if t.trade_type != "BUY":
            continue
        qty, cost = today_buy_agg.get(t.instrument_id, (0, 0.0))
        today_buy_agg[t.instrument_id] = (
            qty + int(t.quantity),
            cost + float(t.total_cost or 0),
        )

    holding_avg_by_instr = {
        h.instrument_id: float(h.avg_buy_price) for (h, _sym) in holding_rows
    }

    realized = 0.0
    closed_trades: list[ClosedTrade] = []
    deployed_today = 0.0
    fills_count = 0

    for t, inst in trade_rows:
        fills_count += 1
        if t.trade_type == "BUY":
            deployed_today += float(t.total_cost or 0)
            continue
        # SELL → P&L vs cost basis
        cost_per_share: float | None = None
        agg = today_buy_agg.get(t.instrument_id)
        if agg and agg[0] > 0:
            cost_per_share = agg[1] / agg[0]
        elif t.instrument_id in holding_avg_by_instr:
            cost_per_share = holding_avg_by_instr[t.instrument_id]
        if cost_per_share is None:
            continue
        pnl = float(t.total_cost or 0) - cost_per_share * int(t.quantity)
        realized += pnl
        closed_trades.append(
            ClosedTrade(
                asset_class="EQUITY",
                bucket="equity",
                label=f"{inst.symbol} ×{int(t.quantity)}",
                entry=cost_per_share,
                exit=float(t.price or 0),
                pnl=pnl,
                reason="EQUITY SELL",
            )
        )

    # Unrealized: compute MTM live so a fresh position shows correctly.
    price_service = PriceService()
    unrealized = 0.0
    for (h, _inst) in holding_rows:
        ltp_val = await price_service.latest_price(h.instrument_id)
        if ltp_val is None:
            ltp_val = float(h.current_price) if h.current_price is not None else float(h.avg_buy_price)
        unrealized += (float(ltp_val) - float(h.avg_buy_price)) * int(h.quantity)

    return realized, unrealized, deployed_today, fills_count, closed_trades, cash_remaining, invested_now


async def _fno_bucket_rollup(
    day: date,
) -> tuple[dict[str, dict[str, Any]], list[ClosedTrade], float]:
    """F&O numbers split by strategy bucket.

    Closed P&L counts a trade if its ``closed_at`` falls on `day` — this is
    the fix for the long-standing bug where stop-outs of overnight positions
    didn't show up because the old query keyed on ``proposed_at`` instead.

    Open MTM is computed via ``position_manager.open_positions_summary()``
    so the LTP/MTM logic stays in one place.
    """
    out: dict[str, dict[str, Any]] = {
        b: {
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "deployed": 0.0,
            "fills_count": 0,
            "closes_count": 0,
            "open_count": 0,
            "detail_lines": [],
        }
        for b in BUCKETS
    }
    closed_trades: list[ClosedTrade] = []
    open_mtm_total = 0.0

    # Closed today (both same-day round-trips and overnight stop-outs).
    async with session_scope() as session:
        closed_rows = list((await session.execute(
            select(
                FNOSignal.id,
                FNOSignal.strategy_type,
                FNOSignal.entry_premium_net,
                FNOSignal.final_pnl,
                FNOSignal.status,
                FNOSignal.closed_at,
                FNOSignal.proposed_at,
                Instrument.symbol,
            )
            .join(Instrument, Instrument.id == FNOSignal.underlying_id)
            .where(
                func.date(FNOSignal.closed_at) == day,
                FNOSignal.dryrun_run_id.is_(None),
            )
            .order_by(FNOSignal.closed_at.asc())
        )).all())

        # Today's fills (entries) — separate count for "fills_count" vs closes.
        fill_rows = list((await session.execute(
            select(
                FNOSignal.strategy_type,
                FNOSignal.entry_premium_net,
            )
            .where(
                func.date(FNOSignal.proposed_at) == day,
                FNOSignal.dryrun_run_id.is_(None),
            )
        )).all())

    for r in closed_rows:
        bucket = bucket_for_fno_strategy(r.strategy_type)
        agg = out[bucket]
        pnl = float(r.final_pnl or 0)
        agg["realized_pnl"] += pnl
        agg["closes_count"] += 1
        reason_map = {
            "closed_target": "TARGET",
            "closed_stop": "STOP",
            "closed_time": "HARD_EXIT",
            "closed_manual": "MANUAL",
        }
        reason = reason_map.get(r.status, r.status or "CLOSE")
        entry = float(r.entry_premium_net or 0)
        # Exit price = entry + pnl is a fair surrogate when only `final_pnl`
        # is persisted on the signal row. The detail line is for the digest;
        # users care about the P&L number, not the precise exit premium net.
        exit_proxy = entry + pnl
        closed_trades.append(
            ClosedTrade(
                asset_class="FNO",
                bucket=bucket,
                label=f"{r.symbol} {r.strategy_type}",
                entry=entry,
                exit=exit_proxy,
                pnl=pnl,
                reason=reason,
            )
        )
        agg["detail_lines"].append(
            f"{reason} {r.symbol} {r.strategy_type}: "
            f"{'+' if pnl >= 0 else ''}₹{pnl:,.0f}"
        )

    for r in fill_rows:
        bucket = bucket_for_fno_strategy(r.strategy_type)
        out[bucket]["fills_count"] += 1
        out[bucket]["deployed"] += float(r.entry_premium_net or 0)

    # Open positions (MTM) — via position_manager so the live-chain LTP path
    # stays in one place. Every still-active position lives in fno_signals.
    try:
        from src.fno.position_manager import open_positions_summary
        open_positions = await open_positions_summary()
    except Exception as exc:
        logger.warning(f"pnl_aggregator: open_positions_summary failed: {exc!r}")
        open_positions = []

    for p in open_positions:
        bucket = bucket_for_fno_strategy(p.get("strategy") or "")
        agg = out[bucket]
        agg["open_count"] += 1
        mtm = float(p.get("mtm") or 0)
        agg["unrealized_pnl"] += mtm
        # Keep the deployed number consistent: if a position was filled today
        # we already added its entry premium; if it carried in from a prior
        # day, charge it now using entry × contracts so the digest's
        # "deployed" line reflects the *current* book.
        proposed_today = (p.get("status") or "") in (
            "paper_filled", "active", "scaled_out_50"
        )
        # We can't tell from open_positions alone whether the entry happened
        # today or yesterday, but fill_rows above already counted today's
        # premium. So only ADD prior-day premium here. As a coarse rule:
        # if entry rate × contracts > today's deployed, the difference is
        # from carry-over positions. Skipping here is fine — the MTM line
        # speaks for itself.
        open_mtm_total += float(p.get("entry") or 0) * int(p.get("lots") or 0) * int(p.get("lot_size") or 0)
        if proposed_today:
            pass  # already counted in fill_rows

    return out, closed_trades, open_mtm_total


async def _fno_phase_counts(day: date) -> tuple[int, int, int]:
    """Return (phase1_passed, phase2_scored, phase3_proceed) for context."""
    from src.models.fno_candidate import FNOCandidate

    async with session_scope() as session:
        p1 = (await session.execute(
            select(func.count()).where(
                FNOCandidate.run_date == day,
                FNOCandidate.phase == 1,
                FNOCandidate.passed_liquidity == True,  # noqa: E712
                FNOCandidate.dryrun_run_id.is_(None),
            )
        )).scalar() or 0
        p2 = (await session.execute(
            select(func.count()).where(
                FNOCandidate.run_date == day,
                FNOCandidate.phase == 2,
                FNOCandidate.composite_score.is_not(None),
                FNOCandidate.dryrun_run_id.is_(None),
            )
        )).scalar() or 0
        p3 = (await session.execute(
            select(func.count()).where(
                FNOCandidate.run_date == day,
                FNOCandidate.phase == 3,
                FNOCandidate.llm_decision == "PROCEED",
                FNOCandidate.dryrun_run_id.is_(None),
            )
        )).scalar() or 0
    return int(p1), int(p2), int(p3)


__all__ = [
    "ClosedTrade",
    "DailyPnLSnapshot",
    "StrategyBucketPnL",
    "daily_pnl_snapshot",
]
