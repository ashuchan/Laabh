"""Signal lifecycle: notify watchlist subscribers, resolve expired signals."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select, text

from src.db import session_scope
from src.models.instrument import Instrument
from src.models.signal import Signal
from src.models.watchlist import WatchlistItem
from src.services.notification_service import NotificationService


class SignalService:
    """Cross-cutting operations on signals."""

    def __init__(self, notifier: NotificationService | None = None) -> None:
        self.notifier = notifier or NotificationService()

    async def notify_watchlist_signals(self, since_minutes: int = 10) -> int:
        """Find recent signals on watchlisted instruments and emit notifications.

        Insert is atomic per signal: ``INSERT ... WHERE NOT EXISTS`` closes the
        race window where two concurrent runs would both see "no notification"
        and both insert. Only signal_ids whose row was actually created (via
        ``RETURNING``) get pushed to Telegram.

        Returns number of notifications created.
        """
        select_sql = text("""
            SELECT s.id, s.action, s.confidence, s.target_price, s.reasoning,
                   i.symbol, i.company_name, i.id AS instrument_id
            FROM signals s
            JOIN instruments i ON i.id = s.instrument_id
            WHERE s.created_at >= NOW() - make_interval(mins => :mins)
              AND EXISTS (
                  SELECT 1 FROM watchlist_items wi
                  WHERE wi.instrument_id = s.instrument_id
                    AND wi.alert_on_signals = TRUE
              )
              AND NOT EXISTS (
                  SELECT 1 FROM notifications n
                  WHERE n.signal_id = s.id AND n.type = 'signal_alert'
              )
        """)
        insert_sql = text("""
            INSERT INTO notifications (type, priority, title, body, instrument_id, signal_id)
            SELECT 'signal_alert', 'high', :title, :body, :instrument_id, :signal_id
            WHERE NOT EXISTS (
                SELECT 1 FROM notifications
                WHERE signal_id = :signal_id AND type = 'signal_alert'
            )
            RETURNING id
        """)
        count = 0
        async with session_scope() as session:
            rows = await session.execute(select_sql, {"mins": since_minutes})
            signals = list(rows.mappings())

        for row in signals:
            title = f"{row['action']} {row['symbol']}"
            conf = f" (conf {float(row['confidence']):.2f})" if row["confidence"] is not None else ""
            tgt = f"\nTarget: ₹{row['target_price']}" if row["target_price"] else ""
            body = f"{row['company_name']}{conf}{tgt}\n{row['reasoning'] or ''}"
            title = title[:200]
            async with session_scope() as session:
                result = await session.execute(
                    insert_sql,
                    {
                        "title": title,
                        "body": body,
                        "instrument_id": row["instrument_id"],
                        "signal_id": row["id"],
                    },
                )
                inserted = result.scalar()
            if inserted is None:
                continue
            count += 1
        if count:
            await self.notifier.push_pending()
        logger.info(f"signal notifications emitted: {count}")
        return count

    async def resolve_expired(self) -> int:
        """Run the Postgres function that moves past-expiry signals to 'expired'."""
        async with session_scope() as session:
            r = await session.execute(text("SELECT resolve_expired_signals()"))
            return int(r.scalar() or 0)
