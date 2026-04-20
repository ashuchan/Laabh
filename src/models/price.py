"""Price data — tick-level (TimescaleDB hypertable) and daily OHLCV."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class PriceTick(Base):
    """Real-time tick data. Stored as a TimescaleDB hypertable on `timestamp`."""

    __tablename__ = "price_ticks"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    ltp: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    open: Mapped[float | None] = mapped_column(Numeric(12, 2))
    high: Mapped[float | None] = mapped_column(Numeric(12, 2))
    low: Mapped[float | None] = mapped_column(Numeric(12, 2))
    close: Mapped[float | None] = mapped_column(Numeric(12, 2))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    oi: Mapped[int | None] = mapped_column(BigInteger)
    bid_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    ask_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    bid_qty: Mapped[int | None] = mapped_column(Integer)
    ask_qty: Mapped[int | None] = mapped_column(Integer)
    change_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))


class PriceDaily(Base):
    """Daily OHLCV summary for each instrument."""

    __tablename__ = "price_daily"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Numeric(12, 2))
    high: Mapped[float | None] = mapped_column(Numeric(12, 2))
    low: Mapped[float | None] = mapped_column(Numeric(12, 2))
    close: Mapped[float | None] = mapped_column(Numeric(12, 2))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    vwap: Mapped[float | None] = mapped_column(Numeric(12, 2))
    prev_close: Mapped[float | None] = mapped_column(Numeric(12, 2))
    change_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    delivery_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
