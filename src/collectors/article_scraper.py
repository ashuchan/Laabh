"""Full-article text extraction via Playwright — enriches raw_content entries."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select, update

from src.collectors.base import BaseCollector, CollectorResult
from src.db import session_scope
from src.models.content import RawContent

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]


class ArticleScraperCollector(BaseCollector):
    """Scrape full article text for unprocessed items whose body is still a short summary."""

    job_name = "article_scraper"

    def __init__(self, limit: int = 20) -> None:
        super().__init__(source_id=None)
        self.limit = limit

    async def _collect(self) -> CollectorResult:
        result = CollectorResult()
        if async_playwright is None:
            result.errors.append("playwright not installed")
            return result

        async with session_scope() as session:
            rows = await session.execute(
                select(RawContent)
                .where(
                    RawContent.is_processed == False,  # noqa: E712
                    RawContent.url.is_not(None),
                    RawContent.media_type == "article",
                )
                .limit(self.limit)
            )
            items = list(rows.scalars())

        if not items:
            return result

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                )
                for item in items:
                    text = await self._scrape_one(ctx, item.url or "")
                    if text:
                        async with session_scope() as session:
                            await session.execute(
                                update(RawContent)
                                .where(RawContent.id == item.id)
                                .values(content_text=text, content_length=len(text))
                            )
                        result.items_new += 1
                    result.items_fetched += 1
            finally:
                await browser.close()

        return result

    async def _scrape_one(self, ctx, url: str) -> str | None:
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            body = await page.evaluate(
                """() => {
                    const sel = ['article', '[itemprop=articleBody]', '.article-body', '.story-article'];
                    for (const s of sel) {
                      const el = document.querySelector(s);
                      if (el && el.innerText.length > 200) return el.innerText;
                    }
                    return document.body.innerText;
                }"""
            )
            return (body or "").strip() or None
        except Exception as exc:
            logger.warning(f"article scrape failed for {url}: {exc}")
            return None
        finally:
            await page.close()
