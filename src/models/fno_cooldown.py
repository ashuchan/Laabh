"""F&O cooldown tracker — prevents revenge trading after a stop hit."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class FNOCooldown(Base):
    """An active cooldown that blocks new entries for a given underlying."""

    __tablename__ = "fno_cooldowns"

    underlying_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    cooldown_until: Mapped[datetime] = mapped_column(primary_key=True)
    reason: Mapped[str | None] = mapped_column(String(50))
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, default=None)
