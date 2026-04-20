"""Instrument lookup and price routes."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select

from src.db import session_scope
from src.models.instrument import Instrument
from src.models.price import PriceDaily, PriceTick

router = APIRouter(prefix="/instruments", tags=["instruments"])


class InstrumentResponse(BaseModel):
    id: uuid.UUID
    symbol: str
    exchange: str
    company_name: str
    sector: str | None
    market_cap_cr: float | None
    is_fno: bool
    is_index: bool

    class Config:
        from_attributes = True


class PriceResponse(BaseModel):
    instrument_id: uuid.UUID
    ltp: float | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    as_of: datetime | None


@router.get("/", response_model=list[InstrumentResponse])
async def search_instruments(
    q: str | None = Query(None, description="Search by symbol or company name"),
    limit: int = Query(20, le=100),
):
    """Search instruments by symbol or name."""
    async with session_scope() as session:
        query = select(Instrument).where(Instrument.is_active == True)
        if q:
            like = f"%{q.upper()}%"
            query = query.where(
                or_(Instrument.symbol.ilike(like), Instrument.company_name.ilike(like))
            )
        query = query.order_by(Instrument.symbol).limit(limit)
        result = await session.execute(query)
        instruments = result.scalars().all()
    return [InstrumentResponse.model_validate(i) for i in instruments]


@router.get("/{instrument_id}/price", response_model=PriceResponse)
async def get_instrument_price(instrument_id: uuid.UUID):
    """Return the latest price for an instrument."""
    async with session_scope() as session:
        instr = await session.get(Instrument, instrument_id)
        if instr is None:
            raise HTTPException(status_code=404, detail="Instrument not found")

        # Try live tick first
        result = await session.execute(
            select(PriceTick)
            .where(PriceTick.instrument_id == instrument_id)
            .order_by(PriceTick.timestamp.desc())
            .limit(1)
        )
        tick = result.scalar_one_or_none()
        if tick:
            return PriceResponse(
                instrument_id=instrument_id,
                ltp=float(tick.ltp),
                open=float(tick.open) if tick.open else None,
                high=float(tick.high) if tick.high else None,
                low=float(tick.low) if tick.low else None,
                close=None,
                volume=tick.volume,
                as_of=tick.timestamp,
            )

        # Fallback to daily
        result2 = await session.execute(
            select(PriceDaily)
            .where(PriceDaily.instrument_id == instrument_id)
            .order_by(PriceDaily.date.desc())
            .limit(1)
        )
        daily = result2.scalar_one_or_none()
        if daily:
            from datetime import datetime, timezone
            return PriceResponse(
                instrument_id=instrument_id,
                ltp=float(daily.close),
                open=float(daily.open) if daily.open else None,
                high=float(daily.high) if daily.high else None,
                low=float(daily.low) if daily.low else None,
                close=float(daily.close),
                volume=daily.volume,
                as_of=datetime.combine(daily.date, datetime.min.time()).replace(
                    tzinfo=timezone.utc
                ),
            )

    raise HTTPException(status_code=404, detail="No price data available")
