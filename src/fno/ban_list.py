"""Daily F&O ban list collector — fetches SEBI MWPL>95% list from NSE archives."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from typing import Sequence

import httpx
from loguru import logger
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from src.db import session_scope
from src.models.fno_ban import FNOBanList
from src.models.instrument import Instrument

# NSE archive URL pattern — verified as of 2026-04
_BAN_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{ddmmyyyy}.csv"
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Laabh/1.0)",
    "Accept": "text/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}


def _format_date(d: date) -> str:
    """Format date as DDMMYYYY for NSE archive URLs."""
    return d.strftime("%d%m%Y")


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30))
async def _fetch_csv(url: str) -> str:
    """Fetch raw CSV text from NSE with retry."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        return resp.text


def _parse_symbols(csv_text: str) -> list[str]:
    """Extract symbol column from NSE ban-list CSV."""
    symbols: list[str] = []
    reader = csv.reader(io.StringIO(csv_text))
    for row in reader:
        if not row:
            continue
        # First column is the symbol; skip header rows
        sym = row[0].strip().upper()
        if sym and sym not in {"SECURITY", "SYMBOL", "SECURITIES", ""}:
            symbols.append(sym)
    return symbols


async def fetch_today(
    ban_date: date | None = None,
    source: str = "NSE",
) -> int:
    """Fetch today's ban list and upsert into `fno_ban_list`. Returns count inserted."""
    target_date = ban_date or date.today()
    url = _BAN_URL_TEMPLATE.format(ddmmyyyy=_format_date(target_date))
    logger.info(f"ban_list: fetching {url}")

    try:
        csv_text = await _fetch_csv(url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # No ban list for this date (holiday or weekend)
            logger.info(f"ban_list: no file for {target_date} (404 — likely holiday)")
            return 0
        raise
    except Exception as exc:
        logger.error(f"ban_list: fetch failed: {exc}")
        raise

    symbols = _parse_symbols(csv_text)
    if not symbols:
        logger.info(f"ban_list: empty list for {target_date}")
        return 0

    inserted = 0
    async with session_scope() as session:
        for sym in symbols:
            result = await session.execute(
                select(Instrument).where(
                    Instrument.symbol == sym,
                    Instrument.is_active == True,  # noqa: E712
                )
            )
            instrument = result.scalar_one_or_none()
            if instrument is None:
                logger.debug(f"ban_list: unknown symbol {sym!r} — skipping")
                continue

            # Check if already stored (idempotent)
            existing = await session.execute(
                select(FNOBanList).where(
                    FNOBanList.instrument_id == instrument.id,
                    FNOBanList.ban_date == target_date,
                    FNOBanList.source == source,
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue

            session.add(FNOBanList(
                instrument_id=instrument.id,
                ban_date=target_date,
                source=source,
                fetched_at=datetime.now(tz=timezone.utc),
            ))
            inserted += 1

    logger.info(f"ban_list: {inserted} new entries for {target_date} ({len(symbols)} symbols in list)")
    return inserted


async def is_banned(instrument_id: object, ban_date: date | None = None) -> bool:
    """Return True if `instrument_id` is in the ban list for `ban_date`."""
    target_date = ban_date or date.today()
    async with session_scope() as session:
        result = await session.execute(
            select(FNOBanList).where(
                FNOBanList.instrument_id == instrument_id,
                FNOBanList.ban_date == target_date,
            )
        )
        return result.scalar_one_or_none() is not None


async def get_banned_ids(ban_date: date | None = None) -> set[object]:
    """Return the set of instrument UUIDs that are banned on `ban_date`."""
    target_date = ban_date or date.today()
    async with session_scope() as session:
        result = await session.execute(
            select(FNOBanList.instrument_id).where(FNOBanList.ban_date == target_date)
        )
        return {row[0] for row in result.all()}
