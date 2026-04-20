"""Check active signals against current prices — mark hit_target / hit_stoploss / expired."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.instrument import Instrument
from src.models.price import PriceDaily, PriceTick
from src.models.signal import Signal, SignalAutoTrade
from src.services.notification_service import NotificationService


class SignalResolver:
    """Resolves active signals by checking current price vs target / stop-loss."""

    def __init__(self) -> None:
        self._notifier = NotificationService()

    async def resolve_active_signals(self) -> int:
        """Check all active signals. Returns count of resolved signals."""
        resolved = 0
        now = datetime.now(timezone.utc)

        async with session_scope() as session:
            result = await session.execute(
                select(Signal).where(Signal.status == "active")
            )
            signals = result.scalars().all()

        for signal in signals:
            try:
                outcome = await self._check_signal(signal, now)
                if outcome:
                    resolved += 1
            except Exception as exc:
                logger.error(f"signal resolver error for {signal.id}: {exc}")

        logger.info(f"signal resolver: {resolved} resolved out of {len(signals)} active")
        return resolved

    async def _check_signal(self, signal: Signal, now: datetime) -> bool:
        """Returns True if signal was resolved."""
        if signal.expiry_date and signal.expiry_date < now:
            await self._resolve(signal, "expired", None, now)
            return True

        ltp = await self._get_ltp(signal.instrument_id)
        if ltp is None:
            return False

        outcome_status = None

        if signal.action == "BUY":
            if signal.target_price and ltp >= Decimal(str(signal.target_price)):
                outcome_status = "hit_target"
            elif signal.stop_loss and ltp <= Decimal(str(signal.stop_loss)):
                outcome_status = "hit_stoploss"
        elif signal.action == "SELL":
            if signal.target_price and ltp <= Decimal(str(signal.target_price)):
                outcome_status = "hit_target"
            elif signal.stop_loss and ltp >= Decimal(str(signal.stop_loss)):
                outcome_status = "hit_stoploss"

        if outcome_status:
            await self._resolve(signal, outcome_status, ltp, now)
            return True
        return False

    async def _resolve(
        self,
        signal: Signal,
        status: str,
        outcome_price: Decimal | None,
        now: datetime,
    ) -> None:
        entry = Decimal(str(signal.entry_price or signal.current_price_at_signal or 0))
        pnl_pct = None
        if outcome_price and entry:
            if signal.action == "BUY":
                pnl_pct = float((outcome_price - entry) / entry * 100)
            else:
                pnl_pct = float((entry - outcome_price) / entry * 100)

        async with session_scope() as session:
            s = await session.get(Signal, signal.id)
            if s:
                s.status = status
                s.outcome_price = float(outcome_price) if outcome_price else None
                s.outcome_date = now
                s.outcome_pnl_pct = pnl_pct
                if signal.signal_date:
                    s.days_to_outcome = (now - signal.signal_date).days

            # Also resolve any auto-trades for this signal
            result = await session.execute(
                select(SignalAutoTrade).where(
                    SignalAutoTrade.signal_id == signal.id,
                    SignalAutoTrade.status == "active",
                )
            )
            auto_trades = result.scalars().all()
            for at in auto_trades:
                at.status = status
                at.exit_price = float(outcome_price) if outcome_price else None
                at.pnl_pct = pnl_pct
                at.resolved_at = now
                if at.created_at:
                    at.days_held = (now - at.created_at).days

        # Notify
        instr = await self._get_instrument(signal.instrument_id)
        symbol = instr.symbol if instr else "UNKNOWN"
        emoji = "🎯" if status == "hit_target" else ("🛑" if status == "hit_stoploss" else "⏰")
        body = f"{emoji} Signal {status.upper()}: {signal.action} {symbol}"
        if outcome_price:
            body += f" @ ₹{outcome_price:,.2f}"
        if pnl_pct is not None:
            body += f" ({pnl_pct:+.2f}%)"
        await self._notifier.create(
            type_="target_hit" if status == "hit_target" else "stoploss_hit",
            title=f"Signal {status}: {symbol}",
            body=body,
            priority="high",
            instrument_id=signal.instrument_id,
            signal_id=signal.id,
        )
        logger.info(f"signal {signal.id} resolved: {status} @ {outcome_price}")

    async def _get_ltp(self, instrument_id) -> Decimal | None:
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

    async def _get_instrument(self, instrument_id) -> Instrument | None:
        async with session_scope() as session:
            return await session.get(Instrument, instrument_id)
