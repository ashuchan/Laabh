"""Trade execution routes."""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from src.api.schemas.trade import PendingOrderResponse, PlaceOrderRequest, TradeResponse
from src.db import session_scope
from src.models.pending_order import PendingOrder
from src.models.portfolio import Portfolio
from src.models.trade import Trade
from src.trading.engine import TradingEngine
from src.trading.order_book import OrderBook
from src.trading.risk_manager import RiskError

router = APIRouter(prefix="/trades", tags=["trades"])
_engine = TradingEngine()
_order_book = OrderBook()


async def _get_default_portfolio() -> Portfolio:
    async with session_scope() as session:
        result = await session.execute(
            select(Portfolio).where(Portfolio.is_active == True).limit(1)
        )
        p = result.scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="No active portfolio found")
    return p


@router.post("/", response_model=TradeResponse | PendingOrderResponse, status_code=201)
async def place_order(req: PlaceOrderRequest):
    """Execute a market order or place a pending limit/SL order."""
    portfolio = await _get_default_portfolio()

    if req.order_type == "MARKET":
        # Require a current price for simulation — fetch from DB
        from src.models.price import PriceTick, PriceDaily
        ltp = None
        async with session_scope() as session:
            result = await session.execute(
                select(PriceTick.ltp)
                .where(PriceTick.instrument_id == req.instrument_id)
                .order_by(PriceTick.timestamp.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row:
                ltp = Decimal(str(row))
            else:
                result2 = await session.execute(
                    select(PriceDaily.close)
                    .where(PriceDaily.instrument_id == req.instrument_id)
                    .order_by(PriceDaily.date.desc())
                    .limit(1)
                )
                row2 = result2.scalar_one_or_none()
                if row2:
                    ltp = Decimal(str(row2))

        if ltp is None:
            raise HTTPException(status_code=422, detail="No price available for this instrument")

        try:
            trade = await _engine.execute_market_order(
                portfolio_id=portfolio.id,
                instrument_id=req.instrument_id,
                trade_type=req.trade_type,
                quantity=req.quantity,
                current_ltp=ltp,
                signal_id=req.signal_id,
                reason=req.reason,
            )
        except RiskError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return TradeResponse.model_validate(trade)
    else:
        order = await _order_book.place_order(
            portfolio_id=portfolio.id,
            instrument_id=req.instrument_id,
            trade_type=req.trade_type,
            order_type=req.order_type,
            quantity=req.quantity,
            limit_price=float(req.limit_price) if req.limit_price else None,
            trigger_price=float(req.trigger_price) if req.trigger_price else None,
            signal_id=req.signal_id,
        )
        return PendingOrderResponse.model_validate(order)


@router.get("/", response_model=list[TradeResponse])
async def list_trades(limit: int = Query(50, le=200), offset: int = 0):
    """List executed trades."""
    portfolio = await _get_default_portfolio()
    async with session_scope() as session:
        result = await session.execute(
            select(Trade)
            .where(Trade.portfolio_id == portfolio.id)
            .order_by(Trade.executed_at.desc())
            .offset(offset)
            .limit(limit)
        )
        trades = result.scalars().all()
    return [TradeResponse.model_validate(t) for t in trades]


@router.get("/pending", response_model=list[PendingOrderResponse])
async def list_pending_orders():
    """List all pending limit/SL orders."""
    portfolio = await _get_default_portfolio()
    async with session_scope() as session:
        result = await session.execute(
            select(PendingOrder).where(
                PendingOrder.portfolio_id == portfolio.id,
                PendingOrder.status == "pending",
            )
        )
        orders = result.scalars().all()
    return [PendingOrderResponse.model_validate(o) for o in orders]


@router.delete("/pending/{order_id}", status_code=204)
async def cancel_pending_order(order_id: uuid.UUID):
    """Cancel a pending order."""
    from datetime import datetime, timezone
    async with session_scope() as session:
        order = await session.get(PendingOrder, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.status != "pending":
            raise HTTPException(status_code=409, detail=f"Order status is '{order.status}'")
        order.status = "cancelled"
        order.cancelled_at = datetime.now(timezone.utc)
