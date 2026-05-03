"""FII/DII provisional data collector — scrapes NSE for daily buy/sell data.

NSE publishes FII/DII activity at:
  https://www.nseindia.com/api/fiidiiTradeReact

For historical dates, data is fetched from NSE's archive endpoint:
  https://archives.nseindia.com/content/nsccl/fao_participant_vol_DDMMYYYY.csv
  (or the equity market equivalent for cash-market FII/DII flows)

This is fetched post-market (after 6 PM IST) and stored as raw_content
with media_type='fii_dii' for consumption by the F&O catalyst scorer.

Replay note: in replay mode, fetch_yesterday(target_date=D) should be called
with D = replay_date - 1 trading day, because FII/DII data lags by one day.
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
# NSE FII/DII archive: participant-wise F&O turnover by date
# Date format: DDMMYYYY
_NSE_FII_DII_ARCHIVE_URL = (
    "https://archives.nseindia.com/content/nsccl/fao_participant_vol_{ddmmyyyy}.csv"
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}
_FII_DII_SOURCE_NAME = "NSE FII/DII Data"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=3, max=30))
async def _fetch_fii_dii_raw() -> list[dict]:
    """Fetch raw FII/DII JSON from NSE live API."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # NSE requires a cookie from the homepage first
        await client.get("https://www.nseindia.com", headers=_HEADERS)
        resp = await client.get(_NSE_FII_DII_URL, headers=_HEADERS)
        resp.raise_for_status()
        return resp.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
async def _fetch_fii_dii_archive(target_date: date) -> list[dict]:
    """Fetch FII/DII data from NSE archives for a historical date.

    The archive CSV has columns: Client_Type, Buy_Value, Sell_Value, Net_Value, etc.
    Returns records in the same shape as the live API so _parse_fii_dii can consume them.
    """
    ddmmyyyy = target_date.strftime("%d%m%Y")
    url = _NSE_FII_DII_ARCHIVE_URL.format(ddmmyyyy=ddmmyyyy)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        await client.get("https://www.nseindia.com", headers=_HEADERS)
        resp = await client.get(url, headers=_HEADERS)
        if resp.status_code == 404:
            logger.warning(f"fii_dii_collector: archive not available for {target_date} (404) — returning empty")
            return []
        resp.raise_for_status()

    # Parse the CSV into the live-API record shape
    records: list[dict] = []
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return records

    header = [h.strip().lower() for h in lines[0].split(",")]
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < len(header):
            continue
        row = dict(zip(header, [p.strip() for p in parts]))
        client_type = row.get("client_type", "").upper()
        try:
            buy_val = float(row.get("buy_value", 0) or 0)
            sell_val = float(row.get("sell_value", 0) or 0)
        except ValueError:
            continue
        # Map archive category names to live-API shape
        if "FII" in client_type or "FPI" in client_type:
            category = "FII/FPI"
        elif "DII" in client_type:
            category = "DII"
        else:
            continue
        records.append({
            "category": category,
            "buyValue": buy_val,
            "sellValue": sell_val,
            "date": target_date.strftime("%d-%b-%Y"),
        })
    return records


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
    """Fetch FII/DII data and store in raw_content. Returns the parsed summary.

    When target_date is today or None, hits the live NSE API.
    When target_date is in the past, routes to the NSE archive endpoint.
    """
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

    today = date.today()
    is_historical = target_date is not None and target_date < today

    try:
        if is_historical:
            raw_records = await _fetch_fii_dii_archive(target_date)
        else:
            raw_records = await _fetch_fii_dii_raw()
    except Exception as exc:
        logger.error(f"fii_dii_collector: fetch failed: {exc}")
        return None

    summary = _parse_fii_dii(raw_records)
    effective_date = target_date or today
    stamp = (
        datetime(effective_date.year, effective_date.month, effective_date.day, 18, 0, 0, tzinfo=timezone.utc)
        if is_historical
        else datetime.now(tz=timezone.utc)
    )
    date_str = summary.get("date") or effective_date.isoformat()
    h = hashlib.sha256(f"fii_dii:{date_str}".encode()).hexdigest()

    async with session_scope() as session:
        session.add(RawContent(
            source_id=source.id,
            content_hash=h,
            title=f"FII/DII {date_str}: FII net={summary['fii_net_cr']}Cr",
            content_text=json.dumps(summary),
            media_type="fii_dii",
            is_processed=True,
            fetched_at=stamp,
        ))

    logger.info(
        f"fii_dii_collector: FII net={summary['fii_net_cr']}Cr, "
        f"DII net={summary['dii_net_cr']}Cr for {date_str}"
    )
    return summary
