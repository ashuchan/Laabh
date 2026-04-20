"""Core paper-trade execution logic — market orders only (limit/SL via order_book)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from loguru import logger
from sqlalchemy import select, text

from src.db import session_scope
from src.models.portfolio import Holding, Portfolio
from src.models.trade import Trade
from src.models.instrument import Instrument
from src.services.notification_service import NotificationService
from src.trading.risk_manager import RiskManager, RiskError


_TWO = Decimal("0.01")


def _round(v: Decimal) -> Decimal:
    return v.quantize(_TWO, rounding=ROUND_HALF_UP)


class TradingEngine:
    """Executes paper trades with realistic brokerage simulation.

    All monetary arithmetic uses Decimal.
    """

    BROKERAGE_PCT = Decimal("0.0003")   # 0.03%
    BROKERAGE_MAX = Decimal("20")       # ₹20 cap
    STT_DELIVERY_SELL = Decimal("0.001")  # 0.1% on sell
    STT_INTRADAY = Decimal("0.00025")     # 0.025% intraday
    TRANSACTION_CHARGE = Decimal("0.0000345")  # 0.00345% NSE
    GST_ON_BROKERAGE = Decimal("0.18")
    STAMP_DUTY = Decimal("0.00015")  # 0.015% on buy

    def __init__(self) -> None:
        self._risk = RiskManager()
        self._notifier = NotificationService()

    def _calc_charges(
        self,
        trade_type: str,
        quantity: int,
        price: Decimal,
        is_intraday: bool = False,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return (brokerage, stt, other_charges) all in Decimal."""
        turnover = price * quantity

        brokerage = min(turnover * self.BROKERAGE_PCT, self.BROKERAGE_MAX)
        gst = _round(brokerage * self.GST_ON_BROKERAGE)
        brokerage = _round(brokerage + gst)

        if trade_type == "SELL":
            stt = _round(turnover * (self.STT_INTRADAY if is_intraday else self.STT_DELIVERY_SELL))
        else:
            stt = Decimal("0")

        txn = _round(turnover * self.TRANSACTION_CHARGE)
        stamp = _round(turnover * self.STAMP_DUTY) if trade_type == "BUY" else Decimal("0")
        other = txn + stamp

        return brokerage, stt, other

    async def execute_market_order(
        self,
        portfolio_id: uuid.UUID,
        instrument_id: uuid.UUID,
        trade_type: str,
        quantity: int,
        current_ltp: Decimal,
        signal_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> Trade:
        """Execute a market order at current_ltp and persist all state changes atomically."""
        price = _round(current_ltp)
        brokerage, stt, other = self._calc_charges(trade_type, quantity, price)
        charges = brokerage + stt + other
        turnover = price * quantity

        if trade_type == "BUY":
            total_cost = _round(turnover + charges)
            await self._risk.validate_buy(
                str(portfolio_id), str(instrument_id), quantity, price, total_cost
            )
        else:
            total_cost = _round(turnover - charges)
            await self._risk.validate_sell(str(portfolio_id), str(instrument_id), quantity)

        async with session_scope() as session:
            # Create trade record
            trade = Trade(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                signal_id=signal_id,
                trade_type=trade_type,
                order_type="MARKET",
                quantity=quantity,
                price=float(price),
                brokerage=float(brokerage),
                stt=float(stt),
                total_cost=float(total_cost),
                status="open",
                entry_reason=reason,
                executed_at=datetime.now(timezone.utc),
            )
            session.add(trade)

            # Update portfolio cash
            portfolio = await session.get(Portfolio, portfolio_id)
            if portfolio is None:
                raise RiskError(f"Portfolio {portfolio_id} not found")

            cash = Decimal(str(portfolio.current_cash))
            if trade_type == "BUY":
                portfolio.current_cash = float(cash - total_cost)
                portfolio.invested_value = float(
                    Decimal(str(portfolio.invested_value or 0)) + turnover
                )
            else:
                portfolio.current_cash = float(cash + total_cost)
                portfolio.invested_value = float(
                    Decimal(str(portfolio.invested_value or 0)) - turnover
                )

            # Upsert holding
            result = await session.execute(
                select(Holding).where(
                    Holding.portfolio_id == portfolio_id,
                    Holding.instrument_id == instrument_id,
                )
            )
            holding = result.scalar_one_or_none()

            if trade_type == "BUY":
                if holding is None:
                    holding = Holding(
                        portfolio_id=portfolio_id,
                        instrument_id=instrument_id,
                        quantity=quantity,
                        avg_buy_price=float(price),
                        invested_value=float(turnover),
                        first_buy_date=datetime.now(timezone.utc),
                    )
                    session.add(holding)
                else:
                    old_qty = holding.quantity
                    old_inv = Decimal(str(holding.invested_value))
                    new_qty = old_qty + quantity
                    new_inv = old_inv + turnover
                    holding.quantity = new_qty
                    holding.avg_buy_price = float(_round(new_inv / new_qty))
                    holding.invested_value = float(new_inv)
                holding.last_trade_date = datetime.now(timezone.utc)
            else:
                if holding:
                    new_qty = holding.quantity - quantity
                    if new_qty <= 0:
                        await session.delete(holding)
                    else:
                        # proportionally reduce invested_value
                        ratio = Decimal(str(quantity)) / Decimal(str(holding.quantity))
                        holding.quantity = new_qty
                        holding.invested_value = float(
                            Decimal(str(holding.invested_value)) * (1 - ratio)
                        )
                        holding.last_trade_date = datetime.now(timezone.utc)

            await session.flush()
            await session.refresh(trade)

        instr_result = None
        async with session_scope() as session:
            instr_result = await session.get(Instrument, instrument_id)

        symbol = instr_result.symbol if instr_result else str(instrument_id)
        notif_body = (
            f"{trade_type} {quantity} {symbol} @ ₹{price:,.2f} — "
            f"₹{turnover:,.2f} turnover (charges: ₹{charges:,.2f})"
        )
        await self._notifier.create(
            type_="trade_executed",
            title=f"Trade: {trade_type} {symbol}",
            body=notif_body,
            priority="medium",
            instrument_id=instrument_id,
        )

        logger.info(f"trade executed: {trade_type} {quantity}×{symbol} @ ₹{price}")
        return trade
