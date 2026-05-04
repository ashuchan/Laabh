"""Price data queries and watchlist alert checking."""
from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import desc, select, text

from src.db import session_scope
from src.models.instrument import Instrument
from src.models.price import PriceDaily, PriceTick
from src.models.watchlist import WatchlistItem
from src.services.notification_service import NotificationService


class PriceService:
    """Query latest price and evaluate price-alert thresholds."""

    def __init__(self, notifier: NotificationService | None = None) -> None:
        self.notifier = notifier or NotificationService()

    async def latest_price(self, instrument_id: uuid.UUID) -> float | None:
        """Return the most recent price — live tick if available, else daily close.

        Falls back to ``price_daily.close`` so callers (e.g. the equity
        strategist at 09:10 IST) keep working when the WebSocket hasn't yet
        streamed a tick for the instrument.
        """
        async with session_scope() as session:
            row = await session.execute(
                select(PriceTick.ltp)
                .where(PriceTick.instrument_id == instrument_id)
                .order_by(desc(PriceTick.timestamp))
                .limit(1)
            )
            val = row.scalar_one_or_none()
            if val is not None:
                return float(val)
            row = await session.execute(
                select(PriceDaily.close)
                .where(PriceDaily.instrument_id == instrument_id)
                .order_by(desc(PriceDaily.date))
                .limit(1)
            )
            val = row.scalar_one_or_none()
            return float(val) if val is not None else None

    async def check_price_alerts(self) -> int:
        """Check all watchlist price alerts against latest tick; notify if crossed."""
        sql = text("""
            WITH latest AS (
                SELECT DISTINCT ON (instrument_id) instrument_id, ltp
                FROM price_ticks
                WHERE timestamp > NOW() - INTERVAL '15 minutes'
                ORDER BY instrument_id, timestamp DESC
            )
            SELECT wi.id AS wi_id, wi.instrument_id, wi.price_alert_above, wi.price_alert_below,
                   i.symbol, i.company_name, l.ltp
            FROM watchlist_items wi
            JOIN instruments i ON i.id = wi.instrument_id
            JOIN latest l ON l.instrument_id = wi.instrument_id
            WHERE (wi.price_alert_above IS NOT NULL AND l.ltp >= wi.price_alert_above)
               OR (wi.price_alert_below IS NOT NULL AND l.ltp <= wi.price_alert_below)
        """)
        count = 0
        async with session_scope() as session:
            rows = await session.execute(sql)
            triggered = list(rows.mappings())

        for row in triggered:
            ltp = float(row["ltp"])
            if row["price_alert_above"] is not None and ltp >= float(row["price_alert_above"]):
                direction = f"crossed above ₹{row['price_alert_above']}"
            else:
                direction = f"dropped below ₹{row['price_alert_below']}"
            await self.notifier.create(
                type_="price_alert",
                title=f"{row['symbol']} @ ₹{ltp:.2f}",
                body=f"{row['company_name']} {direction}",
                priority="high",
                instrument_id=row["instrument_id"],
            )
            count += 1
        logger.info(f"price alerts triggered: {count}")
        return count
