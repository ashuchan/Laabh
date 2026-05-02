"""Schema mismatches and sustained failures that drive GitHub issue creation."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class ChainCollectionIssue(Base):
    """Tracks schema mismatches and sustained failures per source/instrument."""

    __tablename__ = "chain_collection_issues"
    __table_args__ = (
        CheckConstraint(
            "issue_type IN ('schema_mismatch','sustained_failure','auth_error')"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id")
    )
    issue_type: Mapped[str] = mapped_column(String(30), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_response: Mapped[str | None] = mapped_column(Text)  # truncated to 8 KB
    detected_at: Mapped[datetime | None] = mapped_column()
    github_issue_url: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column()
    resolved_by: Mapped[str | None] = mapped_column(String(50))
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
