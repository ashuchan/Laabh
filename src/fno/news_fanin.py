"""Per-symbol news fan-in for Phase 2 → Phase 3 transition.

Phase 3 historically saw `(no recent headlines)` for most candidates because
the global RSS sweep is broad-coverage but doesn't guarantee a specific stock
appears. This module fetches a targeted Google News query per top-N
Phase 2 candidate, persists new articles to `raw_content`, and returns the
inserted IDs so the caller can immediately re-run the LLM extractor.

The output: every Phase 3 candidate has at least N stock-specific headlines
in `signals` (created by extractor) so the thesis prompt has narrative.

Cost shape: one Google News RSS call per symbol (free). LLM cost is incurred
only via the existing extractor — typically 0-3 articles per symbol → ~30 calls
per day for top-30 candidates. ~$0.20-0.40/day at Haiku rates.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from datetime import date, datetime, timezone
from typing import Iterable

import feedparser
import httpx
from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.content import RawContent
from src.models.fno_candidate import FNOCandidate
from src.models.instrument import Instrument
from src.models.source import DataSource


_GOOGLE_NEWS_TEMPLATE = (
    "https://news.google.com/rss/search"
    "?q={query}+stock&hl=en-IN&gl=IN&ceid=IN:en"
)
_HEADERS = {"User-Agent": "Mozilla/5.0 Laabh/1.0"}


async def _fetch_query(query: str) -> str:
    url = _GOOGLE_NEWS_TEMPLATE.format(query=urllib.parse.quote(query))
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


async def _ensure_per_symbol_source(session) -> "DataSource":
    """Lookup or create the synthetic data_source row used to tag fan-in items."""
    result = await session.execute(
        select(DataSource).where(DataSource.name == "Google News - Per Symbol")
    )
    src = result.scalar_one_or_none()
    if src is not None:
        return src
    src = DataSource(
        name="Google News - Per Symbol",
        type="web_scraper",
        status="active",
        config={"url": "https://news.google.com/rss/search"},
    )
    session.add(src)
    await session.flush()
    return src


def _content_hash(title: str, link: str | None) -> str:
    """SHA-256 over title + link, matching RSSCollector's dedup behaviour."""
    import hashlib
    raw = (title.strip() + "|" + (link or "")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def _persist_entries(
    *,
    session,
    source_id,
    symbol: str,
    entries: Iterable,
) -> int:
    """Insert deduplicated entries and return count of NEW rows.

    Uses no_autoflush so the dedup SELECT doesn't prematurely flush
    half-built RawContent rows from earlier iterations of the same batch.
    """
    new_count = 0
    seen_in_batch: set[str] = set()

    with session.no_autoflush:
        for entry in entries:
            title = getattr(entry, "title", None)
            link = getattr(entry, "link", None)
            if not title:
                continue
            h = _content_hash(title, link)
            if h in seen_in_batch:
                continue
            exists = await session.execute(
                select(RawContent.id).where(RawContent.content_hash == h)
            )
            if exists.scalar_one_or_none():
                continue

            summary = getattr(entry, "summary", None) or getattr(entry, "description", "") or title
            # Tag the symbol explicitly so the LLM has it even if the title
            # uses a friendly name (e.g. "Sun Pharma" vs canonical SUNPHARMA).
            tagged = f"[Stock: {symbol}]\n{summary}"

            published = None
            from time import struct_time
            for attr in ("published_parsed", "updated_parsed"):
                t = getattr(entry, attr, None)
                if t and isinstance(t, struct_time):
                    try:
                        published = datetime(*t[:6], tzinfo=timezone.utc)
                        break
                    except Exception:
                        continue

            ext_id = getattr(entry, "id", None)
            session.add(RawContent(
                source_id=source_id,
                content_hash=h,
                # external_id is a VARCHAR(500); Google News URLs can exceed that.
                external_id=(ext_id[:500] if ext_id else None),
                title=title[:200],
                content_text=tagged[:8000],
                # url likewise — schema allows long but truncate defensively.
                url=(link[:1000] if link else None),
                author=(getattr(entry, "author", None) or "")[:200] or None,
                published_at=published,
                media_type="article",
                content_length=len(tagged),
            ))
            seen_in_batch.add(h)
            new_count += 1
    return new_count


async def fan_in_for_phase2(
    run_date: date | None = None,
    *,
    top_n: int = 30,
) -> dict:
    """Pull a per-symbol Google News query for the top-N Phase 2 candidates.

    Returns a dict with new article counts per symbol.
    """
    if run_date is None:
        run_date = date.today()

    async with session_scope() as session:
        rows = await session.execute(
            select(FNOCandidate.composite_score, Instrument.symbol)
            .join(Instrument, Instrument.id == FNOCandidate.instrument_id)
            .where(
                FNOCandidate.run_date == run_date,
                FNOCandidate.phase == 2,
                FNOCandidate.dryrun_run_id.is_(None),
            )
            .order_by(FNOCandidate.composite_score.desc().nulls_last())
            .limit(top_n)
        )
        candidates = [r.symbol for r in rows.all()]

    if not candidates:
        logger.info("fno.news_fanin: no Phase 2 candidates — skipping")
        return {"candidates": 0, "articles": 0}

    logger.info(f"fno.news_fanin: querying news for {len(candidates)} symbols")

    by_symbol: dict[str, int] = {}
    total_new = 0

    async with session_scope() as session:
        source = await _ensure_per_symbol_source(session)
        source_id = source.id

    for symbol in candidates:
        try:
            body = await _fetch_query(symbol)
            feed = await asyncio.to_thread(feedparser.parse, body)
            entries = feed.entries[:5]  # cap per symbol
            async with session_scope() as session:
                added = await _persist_entries(
                    session=session, source_id=source_id,
                    symbol=symbol, entries=entries,
                )
            by_symbol[symbol] = added
            total_new += added
            logger.debug(f"  {symbol:12s}: {added} new articles")
        except Exception as exc:
            logger.warning(f"fno.news_fanin: {symbol} failed: {exc}")
            by_symbol[symbol] = 0
        # be polite to Google News
        await asyncio.sleep(0.4)

    logger.info(
        f"fno.news_fanin: total {total_new} new articles across {len(candidates)} symbols"
    )
    return {"candidates": len(candidates), "articles": total_new, "by_symbol": by_symbol}
