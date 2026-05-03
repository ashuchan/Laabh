"""Options chain snapshot model."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String  # noqa: F401
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class OptionsChain(Base):
    """One row per (instrument, snapshot_time, expiry, strike, option_type)."""

    __tablename__ = "options_chain"
    __table_args__ = (CheckConstraint("option_type IN ('CE','PE')"),)

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    expiry_date: Mapped[date] = mapped_column(Date, primary_key=True)
    strike_price: Mapped[float] = mapped_column(Numeric(12, 2), primary_key=True)
    option_type: Mapped[str] = mapped_column(String(2), primary_key=True)

    ltp: Mapped[float | None] = mapped_column(Numeric(12, 2))
    bid_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    ask_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    bid_qty: Mapped[int | None] = mapped_column(Integer)
    ask_qty: Mapped[int | None] = mapped_column(Integer)

    volume: Mapped[int | None] = mapped_column(BigInteger)
    oi: Mapped[int | None] = mapped_column(BigInteger)
    oi_change: Mapped[int | None] = mapped_column(BigInteger)

    iv: Mapped[float | None] = mapped_column(Numeric(8, 4))
    delta: Mapped[float | None] = mapped_column(Numeric(8, 4))
    gamma: Mapped[float | None] = mapped_column(Numeric(10, 6))
    theta: Mapped[float | None] = mapped_column(Numeric(10, 4))
    vega: Mapped[float | None] = mapped_column(Numeric(10, 4))

    underlying_ltp: Mapped[float | None] = mapped_column(Numeric(12, 2))
    source: Mapped[str | None] = mapped_column(String(20), default="nse")
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
