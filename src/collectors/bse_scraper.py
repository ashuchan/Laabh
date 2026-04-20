"""BSE corporate announcements via their public JSON API."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from loguru import logger
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, CollectorResult
from src.db import session_scope
from src.models.content import RawContent
from src.models.source import DataSource

BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"


class BSEScraperCollector(BaseCollector):
    """Fetches the latest BSE corporate filings (last 24 hours)."""

    job_name = "bse_scraper"

    async def _collect(self) -> CollectorResult:
        result = CollectorResult()
        async with session_scope() as session:
            row = await session.execute(
                select(DataSource).where(
                    DataSource.name == "BSE Corporate Announcements",
                    DataSource.status == "active",
                )
            )
            source = row.scalar_one_or_none()
        if source is None:
            logger.info("BSE source not configured; skipping")
            return result

        self.source_id = str(source.id)
        try:
            items = await self._fetch_announcements()
        except Exception as exc:
            result.errors.append(str(exc))
            return result

        async with session_scope() as session:
            for item in items:
                title = item.get("HEADLINE") or item.get("NEWSSUB")
                url = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{item.get('ATTACHMENTNAME', '')}"
                if not title:
                    continue
                h = self.content_hash(title, url)
                exists = await session.execute(select(RawContent.id).where(RawContent.content_hash == h))
                if exists.scalar_one_or_none():
                    continue
                session.add(RawContent(
                    source_id=source.id,
                    content_hash=h,
                    external_id=str(item.get("NEWSID") or ""),
                    title=title,
                    content_text=item.get("NEWS_DT", ""),
                    url=url,
                    published_at=_parse_dt(item.get("NEWS_DT")),
                    media_type="filing",
                    author=item.get("SCRIP_CD"),
                ))
                result.items_new += 1
                result.items_fetched += 1
        return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _fetch_announcements(self) -> list[dict]:
        today = datetime.utcnow().date()
        prev = today - timedelta(days=1)
        params = {
            "pageno": "1",
            "strCat": "-1",
            "strPrevDate": prev.strftime("%Y%m%d"),
            "strScrip": "",
            "strSearch": "P",
            "strToDate": today.strftime("%Y%m%d"),
            "strType": "C",
        }
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 Laabh/1.0",
                "Referer": "https://www.bseindia.com/",
            },
        ) as client:
            r = await client.get(BSE_URL, params=params)
            r.raise_for_status()
            data = r.json()
            return data.get("Table", []) or []


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt) + 5], fmt)
        except ValueError:
            continue
    return None
