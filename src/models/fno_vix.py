"""India VIX tick model."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class VIXTick(Base):
    """One India VIX reading per timestamp with regime classification."""

    __tablename__ = "vix_ticks"
    __table_args__ = (CheckConstraint("regime IN ('low','neutral','high')"),)

    timestamp: Mapped[datetime] = mapped_column(primary_key=True)
    vix_value: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    regime: Mapped[str] = mapped_column(String(10), nullable=False)
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, default=None)
