"""F&O ban list model — SEBI MWPL>95% instruments excluded from new positions."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class FNOBanList(Base):
    """One row per banned instrument per day per source."""

    __tablename__ = "fno_ban_list"
    __table_args__ = (UniqueConstraint("instrument_id", "ban_date", "source"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    ban_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(20), server_default="NSE")
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
