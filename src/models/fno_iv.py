"""IV history — daily ATM implied volatility percentile per instrument."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class IVHistory(Base):
    """Daily ATM IV and 52-week percentile for each F&O-eligible instrument."""

    __tablename__ = "iv_history"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    atm_iv: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    iv_rank_52w: Mapped[float | None] = mapped_column(Numeric(6, 2))
    iv_percentile_52w: Mapped[float | None] = mapped_column(Numeric(6, 2))
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, default=None)
