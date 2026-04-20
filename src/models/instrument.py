"""Instruments (stocks, indices, ETFs)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base
from src.models._types import MARKET_SEGMENT


class Instrument(Base):
    """A tradeable instrument (stock, index, or ETF)."""

    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("symbol", "exchange"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    segment: Mapped[str] = mapped_column(MARKET_SEGMENT, nullable=False, server_default="NSE_EQ")
    isin: Mapped[str | None] = mapped_column(String(12))
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(100))
    market_cap_cr: Mapped[float | None] = mapped_column(Numeric(15, 2))
    lot_size: Mapped[int] = mapped_column(Integer, server_default="1")
    tick_size: Mapped[float] = mapped_column(Numeric(6, 4), server_default="0.05")

    angel_one_token: Mapped[str | None] = mapped_column(String(20))
    kite_token: Mapped[str | None] = mapped_column(String(20))
    yahoo_symbol: Mapped[str | None] = mapped_column(String(30))

    is_fno: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_index: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")

    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
