"""Versioned post-mortem lessons surfaced into LLM prompts."""
from __future__ import annotations

import uuid
from datetime import date as date_type, datetime

from sqlalchemy import Boolean, Date, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class StrategyLesson(Base):
    """Short, actionable post-mortem note retrieved into prompt enrichment.

    One row per lesson; rows are append-only. Retire a lesson by flipping
    ``is_active`` to false rather than deleting so the audit trail stays.
    """

    __tablename__ = "strategy_lessons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    lesson_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
