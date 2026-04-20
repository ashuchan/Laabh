"""Analysts — people/sources giving stock tips; scored by signal outcomes."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class Analyst(Base):
    """A market analyst/commentator whose signals we track and score."""

    __tablename__ = "analysts"
    __table_args__ = (UniqueConstraint("normalized_name", "organization"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)

    organization: Mapped[str | None] = mapped_column(String(200))
    designation: Mapped[str | None] = mapped_column(String(200))

    primary_source_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))

    total_signals: Mapped[int] = mapped_column(Integer, server_default="0")
    signals_hit_target: Mapped[int] = mapped_column(Integer, server_default="0")
    signals_hit_sl: Mapped[int] = mapped_column(Integer, server_default="0")
    signals_expired: Mapped[int] = mapped_column(Integer, server_default="0")
    hit_rate: Mapped[float] = mapped_column(Numeric(5, 4), server_default="0")
    avg_return_pct: Mapped[float] = mapped_column(Numeric(8, 4), server_default="0")
    avg_days_to_target: Mapped[float | None] = mapped_column(Numeric(6, 1))
    best_sector: Mapped[str | None] = mapped_column(String(100))

    credibility_score: Mapped[float] = mapped_column(Numeric(5, 3), server_default="0.5")

    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
