"""Daily RBI repo rate, used as the risk-free rate in BS option pricing.

The RBI doesn't change rates daily — typical row count is < 50 per year. The
table stores one row per *announcement* date; consumers read the most recent
row at-or-before the trade date to get the prevailing rate.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class RBIRepoHistory(Base):
    """One row per RBI repo-rate announcement date."""

    __tablename__ = "rbi_repo_history"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    repo_rate_pct: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
