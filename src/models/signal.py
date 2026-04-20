"""Extracted trading signals and auto-trade tracking for source quality eval."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base
from src.models._types import SIGNAL_ACTION, SIGNAL_STATUS, SIGNAL_TIMEFRAME


class Signal(Base):
    """A trading recommendation extracted from content or broker feed."""

    __tablename__ = "signals"
    __table_args__ = (CheckConstraint("confidence BETWEEN 0 AND 1"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    content_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_content.id")
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id"), nullable=False
    )

    action: Mapped[str] = mapped_column(SIGNAL_ACTION, nullable=False)
    timeframe: Mapped[str] = mapped_column(SIGNAL_TIMEFRAME, server_default="short_term")

    entry_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    target_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(12, 2))
    current_price_at_signal: Mapped[float | None] = mapped_column(Numeric(12, 2))

    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    reasoning: Mapped[str | None] = mapped_column(Text)

    analyst_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysts.id")
    )
    analyst_name_raw: Mapped[str | None] = mapped_column(String(200))

    convergence_score: Mapped[int] = mapped_column(Integer, server_default="1")
    related_signal_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))

    status: Mapped[str] = mapped_column(SIGNAL_STATUS, server_default="active")
    outcome_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    outcome_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    days_to_outcome: Mapped[int | None] = mapped_column(Integer)

    signal_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SignalAutoTrade(Base):
    """Auto-executed virtual trade for each signal — powers source/analyst scoring."""

    __tablename__ = "signal_auto_trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.id"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id"), nullable=False
    )
    analyst_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysts.id")
    )

    action: Mapped[str] = mapped_column(SIGNAL_ACTION, nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    target_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(12, 2))

    status: Mapped[str] = mapped_column(SIGNAL_STATUS, server_default="active")
    exit_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    days_held: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
