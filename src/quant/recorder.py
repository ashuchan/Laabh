"""TradeRecorder abstraction — encapsulates per-mode trade & day-state persistence.

Two implementations:
  * ``LiveTradeRecorder``     — writes to ``quant_trades`` and ``quant_day_state``.
  * ``BacktestTradeRecorder`` — writes to ``backtest_trades`` and updates the
    ``backtest_runs`` row.

The orchestrator depends only on the abstract ``TradeRecorder`` contract, so
swapping modes is a one-line dependency injection.

SOLID notes:
  * SRP   — each implementation handles exactly one ledger.
  * OCP   — adding a third mode (e.g. paper-trade vs simulation) requires no
            orchestrator change, only a new recorder.
  * LSP   — both recorders preserve the same return shapes; live and backtest
            paths in the orchestrator are interchangeable.
  * DIP   — orchestrator imports the ABC, not the concrete classes.
"""
from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from src.db import session_scope


# ---------------------------------------------------------------------------
# Open-trade payload (avoids passing 12 positional args at every call site)
# ---------------------------------------------------------------------------

@dataclass
class OpenTradePayload:
    """All fields needed to persist a fresh trade open."""

    portfolio_id: uuid.UUID
    underlying_id: uuid.UUID
    primitive_name: str
    arm_id: str
    direction: str
    entry_at: datetime
    entry_premium_net: Decimal
    estimated_costs: Decimal
    signal_strength_at_entry: float
    posterior_mean_at_entry: float
    sampled_mean_at_entry: float
    bandit_seed: int
    kelly_fraction: float
    lots: int
    legs: dict | None = None


@dataclass
class CloseTradePayload:
    """Fields needed to mark a trade closed."""

    trade_id: uuid.UUID | None
    arm_id: str
    portfolio_id: uuid.UUID
    exit_at: datetime
    exit_premium_net: Decimal
    realized_pnl: Decimal
    exit_reason: str


@dataclass
class DayInitPayload:
    """Per-day setup for the relevant ledger row."""

    portfolio_id: uuid.UUID
    trading_date: date
    starting_nav: float
    universe: list[dict]
    config_snapshot: dict
    bandit_seed: int


@dataclass
class DayFinalizePayload:
    """Per-day teardown — final NAV + circuit-breaker fire times."""

    portfolio_id: uuid.UUID
    trading_date: date
    final_nav: float
    starting_nav: float
    lockin_fired_at: datetime | None
    kill_switch_fired_at: datetime | None
    trade_count: int


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class TradeRecorder(abc.ABC):
    """Persistence facade — orchestrator depends on this, not concrete tables."""

    @abc.abstractmethod
    async def open_trade(self, payload: OpenTradePayload) -> uuid.UUID | None:
        """Persist a new trade row. Returns its UUID, or None on failure."""
        ...

    @abc.abstractmethod
    async def close_trade(self, payload: CloseTradePayload) -> None:
        """Mark a trade closed (exit price, P&L, reason)."""
        ...

    @abc.abstractmethod
    async def init_day(self, payload: DayInitPayload) -> None:
        """Idempotently create the per-day header row."""
        ...

    @abc.abstractmethod
    async def finalize_day(self, payload: DayFinalizePayload) -> None:
        """Update the per-day header row with end-of-day stats."""
        ...


# ---------------------------------------------------------------------------
# Live implementation — quant_trades + quant_day_state
# ---------------------------------------------------------------------------

class LiveTradeRecorder(TradeRecorder):
    """Writes to the live ``quant_trades`` and ``quant_day_state`` tables."""

    async def open_trade(self, p: OpenTradePayload) -> uuid.UUID | None:
        from src.models.quant_trade import QuantTrade

        try:
            async with session_scope() as session:
                trade = QuantTrade(
                    portfolio_id=p.portfolio_id,
                    underlying_id=p.underlying_id,
                    primitive_name=p.primitive_name,
                    arm_id=p.arm_id,
                    direction=p.direction,
                    legs=p.legs or {},
                    entry_at=p.entry_at,
                    entry_premium_net=p.entry_premium_net,
                    estimated_costs=p.estimated_costs,
                    signal_strength_at_entry=p.signal_strength_at_entry,
                    posterior_mean_at_entry=p.posterior_mean_at_entry,
                    sampled_mean_at_entry=p.sampled_mean_at_entry,
                    bandit_seed=p.bandit_seed,
                    kelly_fraction=p.kelly_fraction,
                    lots=p.lots,
                    status="open",
                )
                session.add(trade)
                await session.flush()
                return trade.id
        except Exception as exc:
            logger.error(f"LiveTradeRecorder.open_trade failed for {p.arm_id}: {exc!r}")
            return None

    async def close_trade(self, p: CloseTradePayload) -> None:
        from src.models.quant_trade import QuantTrade

        async with session_scope() as session:
            trade = None
            if p.trade_id is not None:
                trade = await session.get(QuantTrade, p.trade_id)
            if trade is None:
                # Fallback: most-recent open trade for this arm — used when
                # crash-recovery rebuilt a position without its DB id.
                q = (
                    select(QuantTrade)
                    .where(QuantTrade.arm_id == p.arm_id)
                    .where(QuantTrade.portfolio_id == p.portfolio_id)
                    .where(QuantTrade.status == "open")
                    .order_by(QuantTrade.entry_at.desc())
                    .limit(1)
                )
                trade = (await session.execute(q)).scalar_one_or_none()
            if trade is None:
                logger.error(
                    f"LiveTradeRecorder.close_trade: no open trade for {p.arm_id}"
                )
                return
            trade.exit_at = p.exit_at
            trade.exit_premium_net = p.exit_premium_net
            trade.realized_pnl = p.realized_pnl
            trade.exit_reason = p.exit_reason
            trade.status = "closed"

    async def init_day(self, p: DayInitPayload) -> None:
        from src.models.quant_day_state import QuantDayState
        from src.config import get_settings

        settings = get_settings()
        async with session_scope() as session:
            existing = await session.get(QuantDayState, (p.portfolio_id, p.trading_date))
            if existing is not None:
                return
            state = QuantDayState(
                portfolio_id=p.portfolio_id,
                date=p.trading_date,
                starting_nav=p.starting_nav,
                universe=[
                    {"id": str(u["id"]), "symbol": u["symbol"]} for u in p.universe
                ],
                lockin_target_pct=settings.laabh_quant_lockin_target_pct,
                kill_switch_pct=settings.laabh_quant_kill_switch_dd_pct,
                bandit_algo=settings.laabh_quant_bandit_algo,
                forget_factor=settings.laabh_quant_bandit_forget_factor,
            )
            session.add(state)

    async def finalize_day(self, p: DayFinalizePayload) -> None:
        from src.models.quant_day_state import QuantDayState
        from src.models.quant_trade import QuantTrade

        async with session_scope() as session:
            state = await session.get(QuantDayState, (p.portfolio_id, p.trading_date))
            if state is None:
                return
            state.final_nav = p.final_nav
            state.pnl_pct = (
                (p.final_nav - p.starting_nav) / p.starting_nav
                if p.starting_nav else 0.0
            )
            state.lockin_fired_at = p.lockin_fired_at
            state.kill_switch_fired_at = p.kill_switch_fired_at
            state.trade_count = p.trade_count


# ---------------------------------------------------------------------------
# Backtest implementation — backtest_trades + backtest_runs
# ---------------------------------------------------------------------------

class BacktestTradeRecorder(TradeRecorder):
    """Writes to ``backtest_trades`` keyed on a fixed ``backtest_run_id``.

    The matching ``backtest_runs`` row must be created by the BacktestRunner
    *before* the orchestrator starts a day — that's why ``init_day`` is a
    no-op here (the row already exists). ``finalize_day`` updates the
    existing row with EOD numbers.

    Provenance tags (``chain_source``, ``underlying_source``) record which
    data tier produced each trade's entry premium so reports can flag
    synthesized vs real fills.
    """

    def __init__(
        self,
        *,
        backtest_run_id: uuid.UUID,
        chain_source: str = "synthesized",
        underlying_source: str = "dhan_intraday",
    ) -> None:
        self._run_id = backtest_run_id
        self._chain_source = chain_source
        self._underlying_source = underlying_source

    async def open_trade(self, p: OpenTradePayload) -> uuid.UUID | None:
        from src.models.backtest_trade import BacktestTrade

        try:
            async with session_scope() as session:
                trade = BacktestTrade(
                    backtest_run_id=self._run_id,
                    underlying_id=p.underlying_id,
                    primitive_name=p.primitive_name,
                    arm_id=p.arm_id,
                    direction=p.direction,
                    legs=p.legs or {},
                    entry_at=p.entry_at,
                    entry_premium_net=p.entry_premium_net,
                    estimated_costs=p.estimated_costs,
                    signal_strength_at_entry=p.signal_strength_at_entry,
                    posterior_mean_at_entry=p.posterior_mean_at_entry,
                    sampled_mean_at_entry=p.sampled_mean_at_entry,
                    kelly_fraction=p.kelly_fraction,
                    lots=p.lots,
                    chain_source=self._chain_source,
                    underlying_source=self._underlying_source,
                )
                session.add(trade)
                await session.flush()
                return trade.id
        except Exception as exc:
            logger.error(
                f"BacktestTradeRecorder.open_trade failed for {p.arm_id}: {exc!r}"
            )
            return None

    async def close_trade(self, p: CloseTradePayload) -> None:
        from src.models.backtest_trade import BacktestTrade

        async with session_scope() as session:
            trade = None
            if p.trade_id is not None:
                trade = await session.get(BacktestTrade, p.trade_id)
            if trade is None:
                # Fallback path — should rarely fire in backtest because we
                # don't crash-recover mid-replay; included for symmetry with
                # live recorder.
                q = (
                    select(BacktestTrade)
                    .where(BacktestTrade.arm_id == p.arm_id)
                    .where(BacktestTrade.backtest_run_id == self._run_id)
                    .where(BacktestTrade.exit_at.is_(None))
                    .order_by(BacktestTrade.entry_at.desc())
                    .limit(1)
                )
                trade = (await session.execute(q)).scalar_one_or_none()
            if trade is None:
                logger.error(
                    f"BacktestTradeRecorder.close_trade: no open trade for {p.arm_id}"
                )
                return
            trade.exit_at = p.exit_at
            trade.exit_premium_net = p.exit_premium_net
            trade.realized_pnl = p.realized_pnl
            trade.exit_reason = p.exit_reason

    async def init_day(self, p: DayInitPayload) -> None:
        # Backtest day rows are pre-created by BacktestRunner — no-op here.
        return

    async def finalize_day(self, p: DayFinalizePayload) -> None:
        from src.models.backtest_run import BacktestRun
        from src.models.backtest_trade import BacktestTrade

        async with session_scope() as session:
            run = await session.get(BacktestRun, self._run_id)
            if run is None:
                logger.warning(
                    f"BacktestTradeRecorder.finalize_day: backtest_run "
                    f"{self._run_id} not found"
                )
                return
            run.final_nav = Decimal(str(p.final_nav))
            run.pnl_pct = Decimal(str(
                (p.final_nav - p.starting_nav) / p.starting_nav
                if p.starting_nav else 0.0
            ))
            run.completed_at = datetime.now(timezone.utc)
            run.trade_count = p.trade_count

            # Count winners by querying realized_pnl > 0
            wins_q = select(func.count()).where(
                BacktestTrade.backtest_run_id == self._run_id,
                BacktestTrade.realized_pnl > 0,
            )
            run.winning_trades = (await session.execute(wins_q)).scalar() or 0
