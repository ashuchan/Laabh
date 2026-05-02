"""Per-instrument data collection tier (refreshed daily at 6 AM IST)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class FNOCollectionTier(Base):
    """Stores the current tier assignment for each F&O instrument."""

    __tablename__ = "fno_collection_tiers"
    __table_args__ = (CheckConstraint("tier IN (1, 2)"),)

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_volume_5d: Mapped[int | None] = mapped_column(BigInteger)
    last_promoted_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime | None] = mapped_column()
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
