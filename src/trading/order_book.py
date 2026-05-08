"""Pending order management — check limit/SL orders against current prices."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import select

from src.config import get_settings
from src.db import session_scope
from src.models.instrument import Instrument
from src.models.pending_order import PendingOrder
from src.models.price import PriceTick
from src.trading.engine import TradingEngine, _refuse_if_equity_disabled


class OrderBook:
    """Checks pending limit/SL orders against latest price ticks and executes matches."""

    def __init__(self) -> None:
        self._engine = TradingEngine()

    async def check_pending_orders(self) -> int:
        """Check all pending orders against latest prices. Returns number executed."""
        executed = 0
        now = datetime.now(timezone.utc)

        settings = get_settings()
        async with session_scope() as session:
            stmt = select(PendingOrder).where(PendingOrder.status == "pending")
            if not settings.equity_trading_enabled:
                # Skip equity (non-F&O) pending orders so we don't trigger
                # the engine's policy refusal once per tick. The engine
                # still rejects as a backstop if the flag flips between
                # this query and execution.
                stmt = stmt.join(
                    Instrument, PendingOrder.instrument_id == Instrument.id
                ).where(Instrument.is_fno.is_(True))
            result = await session.execute(stmt)
            orders = result.scalars().all()

        for order in orders:
            # Skip expired orders
            if order.valid_till and order.valid_till < now:
                async with session_scope() as session:
                    o = await session.get(PendingOrder, order.id)
                    if o:
                        o.status = "expired"
                        o.cancelled_at = now
                continue

            ltp = await self._get_ltp(order.instrument_id)
            if ltp is None:
                continue

            should_execute = self._should_trigger(order, ltp)
            if not should_execute:
                continue

            try:
                await self._engine.execute_market_order(
                    portfolio_id=order.portfolio_id,
                    instrument_id=order.instrument_id,
                    trade_type=order.trade_type,
                    quantity=order.quantity,
                    current_ltp=ltp,
                    signal_id=order.signal_id,
                    reason=f"Triggered from {order.order_type} order {order.id}",
                )
                async with session_scope() as session:
                    o = await session.get(PendingOrder, order.id)
                    if o:
                        o.status = "executed"
                        o.executed_at = now
                executed += 1
                logger.info(f"pending order {order.id} executed at ₹{ltp}")
            except Exception as exc:
                logger.error(f"failed to execute pending order {order.id}: {exc}")

        return executed

    def _should_trigger(self, order: PendingOrder, ltp: Decimal) -> bool:
        if order.order_type == "LIMIT":
            if order.trade_type == "BUY" and order.limit_price:
                return ltp <= Decimal(str(order.limit_price))
            if order.trade_type == "SELL" and order.limit_price:
                return ltp >= Decimal(str(order.limit_price))
        elif order.order_type in ("STOP_LOSS", "STOP_LOSS_MARKET"):
            if order.trade_type == "BUY" and order.trigger_price:
                return ltp >= Decimal(str(order.trigger_price))
            if order.trade_type == "SELL" and order.trigger_price:
                return ltp <= Decimal(str(order.trigger_price))
        return False

    async def _get_ltp(self, instrument_id: uuid.UUID) -> Decimal | None:
        async with session_scope() as session:
            result = await session.execute(
                select(PriceTick.ltp)
                .where(PriceTick.instrument_id == instrument_id)
                .order_by(PriceTick.timestamp.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return Decimal(str(row)) if row is not None else None

    async def place_order(
        self,
        portfolio_id: uuid.UUID,
        instrument_id: uuid.UUID,
        trade_type: str,
        order_type: str,
        quantity: int,
        limit_price: float | None = None,
        trigger_price: float | None = None,
        signal_id: uuid.UUID | None = None,
        valid_till: datetime | None = None,
    ) -> PendingOrder:
        """Store a new pending limit or stop-loss order."""
        await _refuse_if_equity_disabled(
            instrument_id, trade_type=trade_type, quantity=quantity
        )
        order = PendingOrder(
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            signal_id=signal_id,
            trade_type=trade_type,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            trigger_price=trigger_price,
            valid_till=valid_till,
            status="pending",
        )
        async with session_scope() as session:
            session.add(order)
            await session.flush()
            await session.refresh(order)
        logger.info(f"pending order placed: {order_type} {trade_type} {quantity}×instrument")
        return order
