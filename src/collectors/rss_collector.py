"""RSS feed collector — polls all active rss_feed data_sources."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from loguru import logger
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, CollectorResult
from src.db import session_scope
from src.models.content import RawContent
from src.models.source import DataSource


class RSSCollector(BaseCollector):
    """Poll all active RSS feeds and insert new items into `raw_content`."""

    job_name = "rss_collector"

    async def _collect(self) -> CollectorResult:
        result = CollectorResult()
        async with session_scope() as session:
            rows = await session.execute(
                select(DataSource).where(
                    DataSource.type == "rss_feed",
                    DataSource.status == "active",
                )
            )
            sources = list(rows.scalars())

        for source in sources:
            try:
                count_new = await self._poll_source(source)
                result.items_new += count_new
                result.items_fetched += count_new
            except Exception as exc:
                logger.warning(f"RSS source {source.name} failed: {exc}")
                result.errors.append(f"{source.name}: {exc}")

        return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _fetch(self, url: str) -> str:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 Laabh/1.0"},
            follow_redirects=True,
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text

    async def _poll_source(self, source: DataSource) -> int:
        """Fetch one RSS source; return count of new items inserted."""
        url = source.config.get("url")
        if not url:
            logger.warning(f"RSS source {source.name} has no url; skipping")
            return 0

        body = await self._fetch(url)
        feed = await asyncio.to_thread(feedparser.parse, body)

        new_count = 0
        async with session_scope() as session:
            for entry in feed.entries:
                title = getattr(entry, "title", None)
                link = getattr(entry, "link", None)
                if not title:
                    continue
                h = self.content_hash(title, link)
                exists = await session.execute(
                    select(RawContent.id).where(RawContent.content_hash == h)
                )
                if exists.scalar_one_or_none():
                    continue

                published = _parse_published(entry)
                summary = getattr(entry, "summary", None) or getattr(entry, "description", "")
                session.add(RawContent(
                    source_id=source.id,
                    content_hash=h,
                    external_id=getattr(entry, "id", None),
                    title=title,
                    content_text=summary,
                    url=link,
                    author=getattr(entry, "author", None),
                    published_at=published,
                    media_type="article",
                    content_length=len(summary) if summary else 0,
                ))
                new_count += 1

        logger.info(f"RSS {source.name}: {new_count} new items")
        return new_count


def _parse_published(entry: Any) -> datetime | None:
    """Extract a tz-aware UTC datetime from a feedparser entry."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None
