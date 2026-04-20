"""Meta-paper-trade every signal to track source/analyst quality."""
from __future__ import annotations

import uuid
from decimal import Decimal

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.price import PriceTick, PriceDaily
from src.models.signal import Signal, SignalAutoTrade


class AutoTrader:
    """Creates a SignalAutoTrade record for every new signal (virtual trade for scoring)."""

    async def record_signal(self, signal_id: uuid.UUID) -> SignalAutoTrade | None:
        """Create an auto-trade entry for the given signal at current market price."""
        async with session_scope() as session:
            signal = await session.get(Signal, signal_id)
            if signal is None:
                logger.warning(f"auto_trader: signal {signal_id} not found")
                return None

            ltp = await self._get_ltp(signal.instrument_id)
            if ltp is None:
                logger.warning(f"auto_trader: no price for instrument {signal.instrument_id}")
                return None

            auto = SignalAutoTrade(
                signal_id=signal.id,
                instrument_id=signal.instrument_id,
                source_id=signal.source_id,
                analyst_id=signal.analyst_id,
                action=signal.action,
                entry_price=float(ltp),
                target_price=signal.target_price,
                stop_loss=signal.stop_loss,
                status="active",
            )
            session.add(auto)
            await session.flush()
            await session.refresh(auto)

        logger.debug(f"auto_trade created for signal {signal_id} @ ₹{ltp}")
        return auto

    async def _get_ltp(self, instrument_id: uuid.UUID) -> Decimal | None:
        async with session_scope() as session:
            result = await session.execute(
                select(PriceTick.ltp)
                .where(PriceTick.instrument_id == instrument_id)
                .order_by(PriceTick.timestamp.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        if row is not None:
            return Decimal(str(row))
        async with session_scope() as session:
            result = await session.execute(
                select(PriceDaily.close)
                .where(PriceDaily.instrument_id == instrument_id)
                .order_by(PriceDaily.date.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return Decimal(str(row)) if row is not None else None
