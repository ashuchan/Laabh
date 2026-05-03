"""Source health tracker for the chain ingestion source-pluggable abstraction."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class SourceHealth(Base):
    """One row per data source — tracks health status and error counts."""

    __tablename__ = "source_health"
    __table_args__ = (
        CheckConstraint("status IN ('healthy','degraded','failed')"),
    )

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="healthy")
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
