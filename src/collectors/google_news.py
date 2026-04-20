"""Google News RSS aggregation — sub-class of RSSCollector for tagged queries."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select

from src.collectors.base import CollectorResult
from src.collectors.rss_collector import RSSCollector
from src.db import session_scope
from src.models.source import DataSource


class GoogleNewsCollector(RSSCollector):
    """Polls only the `Google News - Indian Stocks` source."""

    job_name = "google_news_collector"

    async def _collect(self) -> CollectorResult:
        result = CollectorResult()
        async with session_scope() as session:
            row = await session.execute(
                select(DataSource).where(
                    DataSource.name == "Google News - Indian Stocks",
                    DataSource.status == "active",
                )
            )
            source = row.scalar_one_or_none()

        if source is None:
            logger.info("Google News source not configured; skipping")
            return result

        try:
            count = await self._poll_source(source)
            result.items_new += count
            result.items_fetched += count
        except Exception as exc:
            result.errors.append(str(exc))
        return result
