"""Data sources registry, job log, and system config."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base
from src.models._types import SOURCE_STATUS, SOURCE_TYPE


class DataSource(Base):
    """A configurable source of financial content (RSS, API, broker, etc.)."""

    __tablename__ = "data_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(SOURCE_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(SOURCE_STATUS, server_default="active")

    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    extraction_schema: Mapped[dict] = mapped_column(JSONB, server_default="{}")

    poll_interval_sec: Mapped[int] = mapped_column(Integer, server_default="300")
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_errors: Mapped[int] = mapped_column(Integer, server_default="0")

    rate_limit_rpm: Mapped[int] = mapped_column(Integer, server_default="60")
    rate_limit_window: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer, server_default="0")

    total_items_fetched: Mapped[int] = mapped_column(BigInteger, server_default="0")
    total_signals_gen: Mapped[int] = mapped_column(BigInteger, server_default="0")

    priority: Mapped[int] = mapped_column(Integer, server_default="5")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobLog(Base):
    """Per-run log of collector and extractor jobs."""

    __tablename__ = "job_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    items_processed: Mapped[int] = mapped_column(Integer, server_default="0")
    signals_generated: Mapped[int] = mapped_column(Integer, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, default=None)


class SystemConfig(Base):
    """Key-value system config store."""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
