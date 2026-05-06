"""Unwind every paper trade tied to one ``strategy_decisions`` row.

Use case: the LLM allocated badly (e.g. equity-only when options were the
play), you want to rerun the morning job from a clean slate. The script
deletes the trades, refunds cash, and recomputes holdings from whatever
trades remain — so partial portfolios across multiple decision rows still
land in a consistent state.

Usage:
    # Dry run — prints what would happen
    python scripts/revert_strategy_decision.py <decision_id>

    # Apply
    python scripts/revert_strategy_decision.py <decision_id> --confirm

    # Also blank the decision row's executed/skipped counts
    python scripts/revert_strategy_decision.py <decision_id> --confirm --reset-decision

Limits:
    * ``brokerage`` / ``stt`` are refunded — this is a paper system; in real
      life those are sunk costs. If you ever wire this against real broker
      books, gate that behaviour.
    * If a SELL in this decision closed a BUY from a *different* decision
      and a later trade references it via ``closing_trade_id``, the script
      logs and refuses to revert. Resolve manually.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal

from loguru import logger
from sqlalchemy import select, update

from src.db import session_scope
from src.models.portfolio import Holding, Portfolio
from src.models.strategy_decision import StrategyDecision
from src.models.trade import Trade


async def revert(decision_id: str, *, confirm: bool, reset_decision: bool) -> dict:
    try:
        decision_uuid = uuid.UUID(decision_id)
    except ValueError:
        raise SystemExit(f"not a valid uuid: {decision_id}")

    pattern = f"[strategy:{decision_id}]%"

    async with session_scope() as session:
        result = await session.execute(
            select(Trade)
            .where(Trade.entry_reason.like(pattern))
            .order_by(Trade.executed_at.desc())
        )
        trades = list(result.scalars())

        if not trades:
            logger.info(f"no trades found for decision {decision_id}")
            decision = await session.get(StrategyDecision, decision_uuid)
            if decision:
                logger.info(
                    f"decision row exists: type={decision.decision_type} "
                    f"executed={decision.actions_executed} "
                    f"skipped={decision.actions_skipped}"
                )
            return {"reverted": 0, "would_revert": 0}

        portfolio_ids = {t.portfolio_id for t in trades}
        if len(portfolio_ids) != 1:
            raise SystemExit(
                f"trades span multiple portfolios: {portfolio_ids} — refusing"
            )
        portfolio_id = next(iter(portfolio_ids))

        # Safety: refuse to delete a trade that is referenced by a later
        # trade's closing_trade_id (would orphan the FK).
        trade_ids = [t.id for t in trades]
        ref_q = await session.execute(
            select(Trade.id, Trade.closing_trade_id).where(
                Trade.closing_trade_id.in_(trade_ids)
            )
        )
        refs = [(tid, cid) for tid, cid in ref_q.all() if tid not in set(trade_ids)]
        if refs:
            for tid, cid in refs:
                logger.error(
                    f"trade {tid} closes-trade {cid} which we want to revert — "
                    "manual cleanup required"
                )
            raise SystemExit("aborting: external references to revert targets")

        portfolio = await session.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise SystemExit(f"portfolio {portfolio_id} not found")

        cash = Decimal(str(portfolio.current_cash))
        invested = Decimal(str(portfolio.invested_value or 0))

        print(
            f"\nDecision: {decision_id}\n"
            f"Portfolio: {portfolio.name} ({portfolio_id})\n"
            f"Current cash: Rs{cash:,.2f} / invested: Rs{invested:,.2f}\n"
            f"\nTrades to revert ({len(trades)}):"
        )
        for t in trades:
            print(
                f"  {t.executed_at:%Y-%m-%d %H:%M}  {t.trade_type:4s} "
                f"{t.quantity:>4d} x Rs{t.price:>9,.2f}  total=Rs{t.total_cost:>10,.2f}  "
                f"({t.instrument_id})"
            )

        # Compute net cash/invested deltas before mutating
        cash_delta = Decimal("0")
        invested_delta = Decimal("0")
        affected_instruments: set[uuid.UUID] = set()
        for t in trades:
            qty = t.quantity
            price = Decimal(str(t.price))
            turnover = price * qty
            total_cost = Decimal(str(t.total_cost))
            if t.trade_type == "BUY":
                cash_delta += total_cost
                invested_delta -= turnover
            else:
                cash_delta -= total_cost
                invested_delta += turnover
            affected_instruments.add(t.instrument_id)

        new_cash = cash + cash_delta
        new_invested = invested + invested_delta
        print(
            f"\nNet effect: cash {cash:+,.2f} -> {new_cash:+,.2f}  "
            f"(delta {cash_delta:+,.2f}), invested -> {new_invested:+,.2f}  "
            f"(delta {invested_delta:+,.2f})"
        )

        if not confirm:
            print(
                "\nDry run only. Re-run with --confirm to apply. "
                "Add --reset-decision to also zero the executed/skipped counters."
            )
            return {"reverted": 0, "would_revert": len(trades)}

        # --- mutating phase ---
        portfolio.current_cash = float(new_cash)
        portfolio.invested_value = float(new_invested)

        for t in trades:
            await session.delete(t)
        await session.flush()

        # Recompute each affected holding from remaining trades — robust
        # against partial reverts and avoids drift in invested_value.
        for inst_id in affected_instruments:
            r = await session.execute(
                select(Trade)
                .where(
                    Trade.portfolio_id == portfolio_id,
                    Trade.instrument_id == inst_id,
                )
                .order_by(Trade.executed_at.asc())
            )
            remaining = list(r.scalars())

            net_qty = 0
            net_invested = Decimal("0")
            first_buy: object = None
            last_trade: object = None
            for rt in remaining:
                last_trade = rt.executed_at
                if rt.trade_type == "BUY":
                    if first_buy is None:
                        first_buy = rt.executed_at
                    net_qty += rt.quantity
                    net_invested += Decimal(str(rt.price)) * rt.quantity
                else:  # SELL — proportional reduction, mirrors engine logic
                    if net_qty > 0:
                        ratio = Decimal(rt.quantity) / Decimal(net_qty)
                        net_invested = net_invested * (Decimal("1") - ratio)
                        net_qty -= rt.quantity

            r2 = await session.execute(
                select(Holding).where(
                    Holding.portfolio_id == portfolio_id,
                    Holding.instrument_id == inst_id,
                )
            )
            holding = r2.scalar_one_or_none()

            if net_qty <= 0:
                if holding is not None:
                    await session.delete(holding)
            else:
                avg = float(net_invested / net_qty)
                if holding is None:
                    holding = Holding(
                        portfolio_id=portfolio_id,
                        instrument_id=inst_id,
                        quantity=net_qty,
                        avg_buy_price=avg,
                        invested_value=float(net_invested),
                        first_buy_date=first_buy,
                        last_trade_date=last_trade,
                    )
                    session.add(holding)
                else:
                    holding.quantity = net_qty
                    holding.avg_buy_price = avg
                    holding.invested_value = float(net_invested)
                    if first_buy is not None:
                        holding.first_buy_date = first_buy
                    if last_trade is not None:
                        holding.last_trade_date = last_trade

        if reset_decision:
            await session.execute(
                update(StrategyDecision)
                .where(StrategyDecision.id == decision_uuid)
                .values(actions_executed=0, actions_skipped=0)
            )

    logger.info(
        f"reverted {len(trades)} trades for decision {decision_id}; "
        f"cash {cash:,.2f}→{new_cash:,.2f}"
    )
    return {
        "reverted": len(trades),
        "cash_before": float(cash),
        "cash_after": float(new_cash),
        "invested_before": float(invested),
        "invested_after": float(new_invested),
        "instruments_recomputed": len(affected_instruments),
    }


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("decision_id", help="strategy_decisions.id (uuid)")
    p.add_argument(
        "--confirm",
        action="store_true",
        help="actually apply the revert; without this, dry run only",
    )
    p.add_argument(
        "--reset-decision",
        action="store_true",
        help="also zero actions_executed/actions_skipped on the decision row",
    )
    args = p.parse_args()

    result = await revert(
        args.decision_id,
        confirm=args.confirm,
        reset_decision=args.reset_decision,
    )
    logger.info(f"result: {result}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
