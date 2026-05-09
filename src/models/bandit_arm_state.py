"""ORM model — per-arm posterior state persisted per trading day."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class BanditArmState(Base):
    """Persisted Thompson / LinTS posterior for one (portfolio, underlying, primitive, date)."""

    __tablename__ = "bandit_arm_state"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), primary_key=True
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    primitive_name: Mapped[str] = mapped_column(String(30), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    posterior_mean: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    posterior_var: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    n_observations: Mapped[int] = mapped_column(Integer, server_default="0")

    # LinTS extras — NULL when bandit_algo = "thompson"
    theta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    a_inv: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    b_vector: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_bandit_arm_state_recent", "portfolio_id", "date"),
    )
