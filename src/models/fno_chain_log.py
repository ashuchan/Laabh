"""Per-poll outcome log — one row per underlying per attempted chain snapshot."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class ChainCollectionLog(Base):
    """Records the outcome of every chain collection attempt."""

    __tablename__ = "chain_collection_log"
    __table_args__ = (
        CheckConstraint("status IN ('ok','fallback_used','missed')"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    primary_source: Mapped[str] = mapped_column(String(20), nullable=False)
    fallback_source: Mapped[str | None] = mapped_column(String(20))
    final_source: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    nse_error: Mapped[str | None] = mapped_column(Text)
    dhan_error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
