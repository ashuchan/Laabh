"""SQLAlchemy ORM model for workflow_runs table."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class WorkflowRun(Base):
    """One execution of a named workflow (e.g. predict_today_combined)."""

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    workflow_name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v1")

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    status_extended: Mapped[str | None] = mapped_column(Text)

    triggered_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="scheduled")
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    total_tokens: Mapped[int | None] = mapped_column(
        "total_tokens",
        __import__("sqlalchemy").Integer,
    )
    error: Mapped[str | None] = mapped_column(Text)

    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    experiment_tag: Mapped[str | None] = mapped_column(Text)
    persona_version_overrides: Mapped[dict] = mapped_column(JSONB, server_default="{}")

    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
