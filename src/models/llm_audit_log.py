"""LLM audit log — one row per Claude API call across the entire system."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class LLMAuditLog(Base):
    """Immutable audit record for every LLM call (phase1.extractor, fno.thesis, etc.)."""

    __tablename__ = "llm_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    caller: Mapped[str] = mapped_column(String(50), nullable=False)
    caller_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    model: Mapped[str] = mapped_column(String(50), nullable=False)
    temperature: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    response_parsed: Mapped[dict | None] = mapped_column(JSONB)

    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
