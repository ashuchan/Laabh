"""NSE corporate announcements (via their public JSON API endpoints)."""
from __future__ import annotations

import httpx
from loguru import logger
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, CollectorResult
from src.db import session_scope
from src.models.content import RawContent
from src.models.source import DataSource

NSE_HOME = "https://www.nseindia.com"
NSE_CORP = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"


class NSEScraperCollector(BaseCollector):
    """Scrapes NSE corporate actions (bonuses, splits, dividends, rights)."""

    job_name = "nse_scraper"

    async def _collect(self) -> CollectorResult:
        result = CollectorResult()
        async with session_scope() as session:
            row = await session.execute(
                select(DataSource).where(
                    DataSource.name == "NSE Corporate Actions",
                    DataSource.status == "active",
                )
            )
            source = row.scalar_one_or_none()
        if source is None:
            logger.info("NSE source not configured; skipping")
            return result

        self.source_id = str(source.id)
        try:
            items = await self._fetch()
        except Exception as exc:
            result.errors.append(str(exc))
            return result

        async with session_scope() as session:
            for item in items:
                symbol = item.get("symbol")
                purpose = item.get("subject") or item.get("purpose") or ""
                ex_date = item.get("exDate")
                if not symbol:
                    continue
                title = f"{symbol}: {purpose}"
                url = f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"
                h = self.content_hash(title, f"{url}|{ex_date}")
                exists = await session.execute(select(RawContent.id).where(RawContent.content_hash == h))
                if exists.scalar_one_or_none():
                    continue
                session.add(RawContent(
                    source_id=source.id,
                    content_hash=h,
                    title=title,
                    content_text=str(item),
                    url=url,
                    media_type="filing",
                    author=symbol,
                ))
                result.items_new += 1
                result.items_fetched += 1
        return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _fetch(self) -> list[dict]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": NSE_HOME,
        }
        async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
            # Prime cookies
            await client.get(NSE_HOME)
            r = await client.get(NSE_CORP)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("data", []) or []
