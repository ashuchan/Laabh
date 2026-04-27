"""FII/DII provisional data collector — scrapes NSE for daily buy/sell data.

NSE publishes FII/DII activity at:
  https://www.nseindia.com/api/fiidiiTradeReact

This is fetched post-market (after 6 PM IST) and stored as raw_content
with media_type='fii_dii' for consumption by the F&O catalyst scorer.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from src.db import session_scope
from src.models.content import RawContent
from src.models.source import DataSource
from sqlalchemy import select

_NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Laabh/1.0)",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}
_FII_DII_SOURCE_NAME = "NSE FII/DII Data"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=3, max=30))
async def _fetch_fii_dii_raw() -> list[dict]:
    """Fetch raw FII/DII JSON from NSE API."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # NSE requires a cookie from the homepage first
        await client.get("https://www.nseindia.com", headers=_HEADERS)
        resp = await client.get(_NSE_FII_DII_URL, headers=_HEADERS)
        resp.raise_for_status()
        return resp.json()


def _parse_fii_dii(raw_records: list[dict]) -> dict:
    """Normalise NSE FII/DII records into a summary dict."""
    fii_net = 0.0
    dii_net = 0.0
    fii_buy = 0.0
    fii_sell = 0.0
    dii_buy = 0.0
    dii_sell = 0.0
    record_date: str | None = None

    for rec in raw_records:
        category = (rec.get("category") or "").upper()
        buy_val = float(rec.get("buyValue", 0) or 0)
        sell_val = float(rec.get("sellValue", 0) or 0)
        net = buy_val - sell_val

        if not record_date:
            record_date = rec.get("date")

        if "FII" in category or "FPI" in category:
            fii_buy += buy_val
            fii_sell += sell_val
            fii_net += net
        elif "DII" in category:
            dii_buy += buy_val
            dii_sell += sell_val
            dii_net += net

    return {
        "date": record_date,
        "fii_buy_cr": round(fii_buy, 2),
        "fii_sell_cr": round(fii_sell, 2),
        "fii_net_cr": round(fii_net, 2),
        "dii_buy_cr": round(dii_buy, 2),
        "dii_sell_cr": round(dii_sell, 2),
        "dii_net_cr": round(dii_net, 2),
    }


async def fetch_yesterday(target_date: date | None = None) -> dict | None:
    """Fetch FII/DII data and store in raw_content. Returns the parsed summary."""
    async with session_scope() as session:
        result = await session.execute(
            select(DataSource).where(
                DataSource.name == _FII_DII_SOURCE_NAME,
                DataSource.status == "active",
            )
        )
        source = result.scalar_one_or_none()

    if source is None:
        logger.warning("fii_dii_collector: no active source — skipping")
        return None

    try:
        raw_records = await _fetch_fii_dii_raw()
    except Exception as exc:
        logger.error(f"fii_dii_collector: fetch failed: {exc}")
        return None

    summary = _parse_fii_dii(raw_records)
    now = datetime.now(tz=timezone.utc)
    date_str = summary.get("date") or (target_date or date.today()).isoformat()
    h = hashlib.sha256(f"fii_dii:{date_str}".encode()).hexdigest()

    async with session_scope() as session:
        session.add(RawContent(
            source_id=source.id,
            content_hash=h,
            title=f"FII/DII {date_str}: FII net={summary['fii_net_cr']}Cr",
            content_text=json.dumps(summary),
            media_type="fii_dii",
            is_processed=True,
            fetched_at=now,
        ))

    logger.info(
        f"fii_dii_collector: FII net={summary['fii_net_cr']}Cr, "
        f"DII net={summary['dii_net_cr']}Cr for {date_str}"
    )
    return summary
