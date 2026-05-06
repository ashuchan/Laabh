"""Notifications: persist + deliver to Telegram."""
from __future__ import annotations

import httpx
from loguru import logger
from sqlalchemy import select, update
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import get_settings
from src.db import session_scope
from src.models.notification import Notification

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class NotificationService:
    """Create notifications and push them to Telegram."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def create(
        self,
        *,
        type_: str,
        title: str,
        body: str,
        priority: str = "medium",
        instrument_id=None,
        signal_id=None,
        trade_id=None,
    ) -> None:
        """Insert a notification row and immediately try to push to Telegram."""
        async with session_scope() as session:
            notif = Notification(
                type=type_,
                priority=priority,
                title=title[:200],
                body=body,
                instrument_id=instrument_id,
                signal_id=signal_id,
                trade_id=trade_id,
            )
            session.add(notif)
            await session.flush()
            notif_id = notif.id

        await self.push_pending()
        _ = notif_id

    async def push_pending(self, limit: int = 50) -> int:
        """Push all un-pushed notifications. Returns count delivered."""
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return 0

        async with session_scope() as session:
            rows = await session.execute(
                select(Notification)
                .where(Notification.is_pushed == False)  # noqa: E712
                .order_by(Notification.created_at.asc())
                .limit(limit)
            )
            pending = list(rows.scalars())

        sent = 0
        for n in pending:
            try:
                await self._send_telegram(f"*{n.title}*\n{n.body}")
            except Exception as exc:
                logger.warning(f"telegram push failed for {n.id}: {exc}")
                continue
            async with session_scope() as session:
                from datetime import datetime
                await session.execute(
                    update(Notification)
                    .where(Notification.id == n.id)
                    .values(is_pushed=True, pushed_at=datetime.utcnow(), push_channel="telegram")
                )
            sent += 1
        return sent

    async def send_text(self, text: str, *, parse_mode: str | None = "Markdown") -> None:
        """Send a raw text message directly to Telegram (bypasses DB).

        ``parse_mode`` defaults to legacy ``Markdown`` for back-compat with
        existing callers. FNO formatters in ``src/fno/notifications.py``
        emit MarkdownV2-escaped output and must pass ``"MarkdownV2"``.
        Pass ``None`` to send as plain text — useful when the body contains
        free-form LLM output that may have unbalanced ``_`` / ``*``.
        """
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            logger.debug("send_text: no Telegram credentials configured")
            return
        await self._send_telegram(text, parse_mode=parse_mode)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _send_telegram(self, text: str, *, parse_mode: str | None = "Markdown") -> None:
        url = TELEGRAM_API.format(token=self.settings.telegram_bot_token)
        payload = {
            "chat_id": self.settings.telegram_chat_id,
            "text": text,
        }
        # Telegram treats absence of the field as plain-text. Sending the
        # JSON literal ``null`` works too, but omitting the key is the
        # documented form and avoids any provider-side surprise.
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
