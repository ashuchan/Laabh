"""SQLAlchemy ORM model for agent_runs table."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class AgentRun(Base):
    """One agent invocation within a workflow_run."""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    persona_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v1")
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    model_used: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")

    output: Mapped[dict | None] = mapped_column(JSONB)
    raw_output: Mapped[dict | None] = mapped_column(JSONB)

    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    error: Mapped[str | None] = mapped_column(Text)
    validation_errors: Mapped[list] = mapped_column(JSONB, server_default="[]")

    llm_audit_log_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    iteration_index: Mapped[int] = mapped_column(Integer, server_default="0")
    estimated_input_tokens: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
