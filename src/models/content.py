"""Raw content — every item ingested before LLM processing."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class RawContent(Base):
    """An ingested content item (article, tweet, filing, transcript chunk, etc.)."""

    __tablename__ = "raw_content"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    external_id: Mapped[str | None] = mapped_column(String(500))

    title: Mapped[str | None] = mapped_column(Text)
    content_text: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(2000))
    author: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    language: Mapped[str] = mapped_column(String(10), server_default="en")
    content_length: Mapped[int | None] = mapped_column(Integer)
    media_type: Mapped[str | None] = mapped_column(String(50))

    is_processed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[str | None] = mapped_column(Text)

    extraction_result: Mapped[dict | None] = mapped_column(JSONB)
    extraction_model: Mapped[str | None] = mapped_column(String(50))
    extraction_tokens: Mapped[int | None] = mapped_column(Integer)
    extraction_cost_usd: Mapped[float | None] = mapped_column(Numeric(8, 6))

    simhash: Mapped[int | None] = mapped_column(BigInteger)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
