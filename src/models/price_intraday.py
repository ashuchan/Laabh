"""3-min intraday OHLCV bars for the F&O universe.

Stored as a TimescaleDB hypertable on ``timestamp`` (chunk = 7 days), matching
the convention used by ``price_ticks``. Used by the backtest harness as the
primary source for Tier 1 (intraday underlying) data.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class PriceIntraday(Base):
    """One 3-min bar for an F&O underlying at a given UTC timestamp."""

    __tablename__ = "price_intraday"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )

    open: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vwap: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    __table_args__ = (
        Index("idx_price_intraday_recent", "instrument_id", "timestamp"),
    )
