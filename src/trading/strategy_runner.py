"""Glue between the LLM strategist and the trading engine.

The strategist is pure-decision; the runner is impure: it executes the
proposed actions, sends a Telegram message per fill, and updates the
``strategy_decisions`` row with the executed/skipped counts so the daily
report can explain what the brain proposed vs. what actually happened.

There is one entry point per scheduler hook:

* :func:`run_morning_allocation`  — 09:10 IST job
* :func:`run_intraday_action`     — every ~hour 09:45–14:30 IST
* :func:`run_eod_squareoff`       — 15:20 IST job

Each performs:
    1. Bootstrap or top up the strategy portfolio (lumpsum vs. SIP).
    2. Ask the strategist to decide.
    3. Execute non-HOLD actions through ``TradingEngine``.
    4. Send a per-fill Telegram message and a digest summary.
    5. Update the decision row with executed/skipped tallies.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from src.config import get_settings
from src.db import session_scope
from src.models.portfolio import Portfolio
from src.models.strategy_decision import StrategyDecision
from src.services.notification_service import NotificationService
from src.services.price_service import PriceService
from src.trading.engine import TradingEngine
from src.trading.equity_strategist import (
    DECISION_EOD,
    DECISION_INTRADAY,
    DECISION_MORNING,
    EquityStrategist,
)
from src.trading.risk_manager import RiskError

STRATEGY_PORTFOLIO_NAME = "Equity Strategy"


# ---------------------------------------------------------------- bootstrap


async def ensure_strategy_portfolio() -> uuid.UUID:
    """Return the id of the persistent equity-strategy portfolio.

    Creates it on first run with the configured initial capital. In ``sip``
    mode the initial cash is the daily budget; in ``lumpsum`` it's the
    configured lumpsum capital.
    """
    settings = get_settings()
    mode = settings.equity_strategy_mode
    initial = (
        settings.equity_strategy_lumpsum_capital
        if mode == "lumpsum"
        else settings.equity_strategy_daily_budget
    )

    async with session_scope() as session:
        result = await session.execute(
            select(Portfolio).where(Portfolio.name == STRATEGY_PORTFOLIO_NAME)
        )
        portfolio = result.scalar_one_or_none()
        if portfolio is None:
            portfolio = Portfolio(
                name=STRATEGY_PORTFOLIO_NAME,
                initial_capital=float(initial),
                current_cash=float(initial),
            )
            session.add(portfolio)
            await session.flush()
            logger.info(
                f"created {STRATEGY_PORTFOLIO_NAME} portfolio "
                f"({mode}, ₹{initial:,.0f})"
            )
        return portfolio.id


async def topup_daily_budget(portfolio_id: uuid.UUID) -> float:
    """Apply the morning cash refill rule for the strategy mode.

    * ``sip``: add ``equity_strategy_daily_budget`` to ``current_cash``;
      un-deployed cash from prior days rolls forward.
    * ``lumpsum``: no-op — capital is set once at bootstrap.

    Returns the new ``current_cash`` balance.
    """
    settings = get_settings()
    if settings.equity_strategy_mode != "sip":
        async with session_scope() as session:
            p = await session.get(Portfolio, portfolio_id)
            return float(p.current_cash if p else 0)

    budget = Decimal(str(settings.equity_strategy_daily_budget))
    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        if p is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")
        new_cash = Decimal(str(p.current_cash or 0)) + budget
        p.current_cash = float(new_cash)
        new_initial = Decimal(str(p.initial_capital or 0)) + budget
        p.initial_capital = float(new_initial)
        logger.info(
            f"sip topup: +₹{budget} → cash=₹{new_cash} (initial=₹{new_initial})"
        )
        return float(new_cash)


# ---------------------------------------------------------------- execution


async def _execute_actions(
    portfolio_id: uuid.UUID,
    decision_id: uuid.UUID,
    actions: list[dict[str, Any]],
) -> tuple[int, int, list[str]]:
    """Run each action through TradingEngine. Returns (executed, skipped, lines)."""
    engine = TradingEngine()
    price_service = PriceService()
    executed = 0
    skipped = 0
    lines: list[str] = []

    for action in actions:
        kind = action["action"]
        if kind == "HOLD":
            continue

        instrument_id_raw = action.get("instrument_id")
        if not instrument_id_raw:
            skipped += 1
            continue
        try:
            instrument_id = uuid.UUID(instrument_id_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue

        qty = int(action.get("qty") or 0)
        if qty <= 0:
            skipped += 1
            continue

        ltp = await price_service.latest_price(instrument_id)
        if ltp is None:
            approx = action.get("approx_price")
            ltp = float(approx) if approx else None
        if ltp is None or ltp <= 0:
            logger.warning(
                f"skip {kind} {action.get('symbol')}: no LTP available"
            )
            skipped += 1
            lines.append(
                f"⚠️ Skipped {kind} {action.get('symbol')} qty={qty}: no LTP"
            )
            continue

        try:
            trade = await engine.execute_market_order(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                trade_type=kind,
                quantity=qty,
                current_ltp=Decimal(str(ltp)),
                signal_id=action.get("signal_id"),
                reason=f"[strategy:{decision_id}] {action.get('reason') or ''}"[:1000],
            )
        except (RiskError, Exception) as exc:
            skipped += 1
            logger.warning(
                f"strategy action skipped: {kind} {action.get('symbol')} "
                f"qty={qty}: {exc}"
            )
            lines.append(
                f"⚠️ Skipped {kind} {action.get('symbol')} qty={qty}: {exc}"
            )
            continue

        executed += 1
        lines.append(
            f"✅ {kind} {qty} {action.get('symbol')} @ ₹{trade.price:,.2f} — "
            f"_{action.get('reason') or 'no reason'}_"
        )
    return executed, skipped, lines


async def _update_decision_counts(
    decision_id: uuid.UUID, executed: int, skipped: int
) -> None:
    async with session_scope() as session:
        await session.execute(
            update(StrategyDecision)
            .where(StrategyDecision.id == decision_id)
            .values(actions_executed=executed, actions_skipped=skipped)
        )


async def _send_summary(
    *,
    title: str,
    reasoning: str,
    cash_after: float,
    lines: list[str],
) -> None:
    notifier = NotificationService()
    body_parts = [f"*{title}*"]
    if reasoning:
        body_parts.append(f"_{reasoning[:600]}_")
    body_parts.append("")
    if lines:
        body_parts.extend(lines)
    else:
        body_parts.append("No actions taken.")
    body_parts.append("")
    body_parts.append(f"Cash remaining: ₹{cash_after:,.2f}")
    text = "\n".join(body_parts)
    await notifier.send_text(text[:3500])


# --------------------------------------------------------------- entrypoints


async def run_morning_allocation(
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Top up daily cash, ask the LLM how to deploy, and execute BUYs."""
    settings = get_settings()
    if not settings.equity_strategy_enabled:
        logger.info("equity strategy disabled — morning job skipped")
        return {"skipped": True}

    portfolio_id = await ensure_strategy_portfolio()
    # In dryrun, do not mutate the live portfolio's cash/initial_capital —
    # just read the current cash for the LLM snapshot.
    if dryrun_run_id is None:
        cash = await topup_daily_budget(portfolio_id)
    else:
        async with session_scope() as session:
            p = await session.get(Portfolio, portfolio_id)
            cash = float(p.current_cash or 0) if p else 0
        logger.info(f"dryrun: skipping sip topup, snapshot cash=₹{cash}")

    strategist = EquityStrategist()
    decision = await strategist.decide_morning_allocation(
        portfolio_id=portfolio_id, as_of=as_of, dryrun_run_id=dryrun_run_id
    )
    actions = decision["actions"]
    executed, skipped, lines = await _execute_actions(
        portfolio_id, decision["decision_id"], actions
    )
    await _update_decision_counts(decision["decision_id"], executed, skipped)

    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        cash_after = float(p.current_cash or 0) if p else cash

    await _send_summary(
        title=f"🌅 Morning Allocation — {date.today():%d %b %Y}",
        reasoning=decision["reasoning"],
        cash_after=cash_after,
        lines=lines,
    )
    logger.info(
        f"morning allocation: executed={executed} skipped={skipped} cash=₹{cash_after}"
    )
    return {
        "decision_id": str(decision["decision_id"]),
        "executed": executed,
        "skipped": skipped,
        "cash_after": cash_after,
    }


async def run_intraday_action(
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Mid-session re-evaluation: hold/sell/buy based on current state."""
    settings = get_settings()
    if not settings.equity_strategy_enabled:
        return {"skipped": True}

    portfolio_id = await ensure_strategy_portfolio()
    if await _intraday_calls_today(portfolio_id) >= settings.equity_strategy_max_intraday_calls:
        logger.info("intraday call cap reached — skipping")
        return {"skipped": True, "reason": "cap_reached"}

    strategist = EquityStrategist()
    decision = await strategist.decide_intraday_action(
        portfolio_id=portfolio_id, as_of=as_of, dryrun_run_id=dryrun_run_id
    )
    actions = [a for a in decision["actions"] if a["action"] != "HOLD"]
    executed, skipped, lines = await _execute_actions(
        portfolio_id, decision["decision_id"], actions
    )
    await _update_decision_counts(decision["decision_id"], executed, skipped)

    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        cash_after = float(p.current_cash or 0) if p else 0

    if executed or skipped:
        await _send_summary(
            title=f"🔄 Intraday Re-eval — {datetime.now(timezone.utc):%H:%M UTC}",
            reasoning=decision["reasoning"],
            cash_after=cash_after,
            lines=lines,
        )
    return {
        "decision_id": str(decision["decision_id"]),
        "executed": executed,
        "skipped": skipped,
    }


async def run_eod_squareoff(
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """At ~15:20 IST, ask the LLM which intraday positions to close."""
    settings = get_settings()
    if not settings.equity_strategy_enabled:
        return {"skipped": True}

    portfolio_id = await ensure_strategy_portfolio()
    strategist = EquityStrategist()
    decision = await strategist.decide_eod_squareoff(
        portfolio_id=portfolio_id, as_of=as_of, dryrun_run_id=dryrun_run_id
    )
    sells = [a for a in decision["actions"] if a["action"] == "SELL"]
    executed, skipped, lines = await _execute_actions(
        portfolio_id, decision["decision_id"], sells
    )
    await _update_decision_counts(decision["decision_id"], executed, skipped)

    async with session_scope() as session:
        p = await session.get(Portfolio, portfolio_id)
        cash_after = float(p.current_cash or 0) if p else 0

    await _send_summary(
        title=f"🌇 EOD Square-off — {date.today():%d %b %Y}",
        reasoning=decision["reasoning"],
        cash_after=cash_after,
        lines=lines or ["No square-off needed — positions held overnight."],
    )
    return {
        "decision_id": str(decision["decision_id"]),
        "executed": executed,
        "skipped": skipped,
    }


async def _intraday_calls_today(portfolio_id: uuid.UUID) -> int:
    """Count today's intraday strategy decisions for the cap."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    async with session_scope() as session:
        result = await session.execute(
            select(StrategyDecision.id).where(
                StrategyDecision.portfolio_id == portfolio_id,
                StrategyDecision.decision_type == DECISION_INTRADAY,
                StrategyDecision.as_of >= today_start,
            )
        )
        return len(list(result.scalars()))


__all__ = [
    "DECISION_EOD",
    "DECISION_INTRADAY",
    "DECISION_MORNING",
    "STRATEGY_PORTFOLIO_NAME",
    "ensure_strategy_portfolio",
    "run_eod_squareoff",
    "run_intraday_action",
    "run_morning_allocation",
    "topup_daily_budget",
]
