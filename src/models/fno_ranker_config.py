"""Strike ranker config history — versioned weight sets."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class RankerConfig(Base):
    """Point-in-time snapshot of ranker weights for replay and regression."""

    __tablename__ = "ranker_configs"

    version: Mapped[str] = mapped_column(String(20), primary_key=True)
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deactivated_at: Mapped[datetime | None] = mapped_column()
    notes: Mapped[str | None] = mapped_column(Text)
