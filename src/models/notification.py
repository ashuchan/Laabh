"""User notifications (in-app + push channels)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base
from src.models._types import NOTIFICATION_PRIORITY, NOTIFICATION_TYPE


class Notification(Base):
    """A notification to be delivered to the user (Telegram, push, in-app)."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    type: Mapped[str] = mapped_column(NOTIFICATION_TYPE, nullable=False)
    priority: Mapped[str] = mapped_column(NOTIFICATION_PRIORITY, server_default="medium")

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id")
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.id")
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id")
    )

    is_read: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_pushed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    push_channel: Mapped[str | None] = mapped_column(String(50))

    action_url: Mapped[str | None] = mapped_column(String(500))
    action_data: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
