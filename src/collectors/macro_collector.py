"""Macro data collector — fetches Brent, gold, copper, DXY, US futures via yfinance.

Runs every 15 minutes during the pre-market window (06:00–09:15 IST).
Stores normalised records in `raw_content` with source_type='api_feed' and
media_type='macro'.  The catalyst scorer reads these records to compute
macro-alignment scores.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import yfinance as yf
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from src.db import session_scope
from src.models.content import RawContent
from src.models.source import DataSource
from sqlalchemy import select

# Yahoo Finance tickers for macro instruments
_MACRO_TICKERS: dict[str, str] = {
    "BRENT": "BZ=F",        # Brent crude futures
    "WTI": "CL=F",          # WTI crude futures
    "GOLD": "GC=F",         # Gold futures
    "COPPER": "HG=F",       # Copper futures
    "DXY": "DX-Y.NYB",     # US Dollar index
    "SPX_FUTURES": "ES=F",  # S&P 500 E-mini futures
    "NASDAQ_FUTURES": "NQ=F",  # Nasdaq 100 E-mini futures
    "DOW_FUTURES": "YM=F",  # Dow Jones E-mini futures
}

_MACRO_SOURCE_NAME = "Macro Data (yfinance)"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
def _fetch_ticker(symbol: str) -> dict:
    """Fetch latest price info for a yfinance ticker. Synchronous."""
    ticker = yf.Ticker(symbol)
    info = ticker.fast_info
    return {
        "symbol": symbol,
        "price": getattr(info, "last_price", None),
        "prev_close": getattr(info, "previous_close", None),
        "change_pct": None,
    }


async def collect() -> int:
    """Collect macro data for all configured tickers. Returns count stored."""
    async with session_scope() as session:
        result = await session.execute(
            select(DataSource).where(
                DataSource.name == _MACRO_SOURCE_NAME,
                DataSource.status == "active",
            )
        )
        source = result.scalar_one_or_none()

    if source is None:
        logger.warning("macro_collector: no active data source found — skipping")
        return 0

    now = datetime.now(tz=timezone.utc)
    stored = 0

    for name, ticker_sym in _MACRO_TICKERS.items():
        try:
            data = _fetch_ticker(ticker_sym)
            price = data.get("price")
            prev = data.get("prev_close")
            if price and prev and prev != 0:
                data["change_pct"] = round((price - prev) / prev * 100, 4)

            content_json = json.dumps({"macro_name": name, **data})
            import hashlib
            h = hashlib.sha256(f"{name}:{now.isoformat()}".encode()).hexdigest()

            async with session_scope() as session:
                session.add(RawContent(
                    source_id=source.id,
                    content_hash=h,
                    title=f"Macro: {name} = {price}",
                    content_text=content_json,
                    media_type="macro",
                    is_processed=True,
                    fetched_at=now,
                ))
            stored += 1
        except Exception as exc:
            logger.warning(f"macro_collector: {name} ({ticker_sym}) failed: {exc}")

    logger.info(f"macro_collector: stored {stored} macro records")
    return stored


def get_macro_direction(macro_name: str, change_pct: float) -> str:
    """Return 'bullish', 'bearish', or 'neutral' direction for a macro move."""
    if abs(change_pct) < 0.3:
        return "neutral"
    return "bullish" if change_pct > 0 else "bearish"


# Sector → macro driver mapping (used by catalyst scorer)
SECTOR_MACRO_MAP: dict[str, list[str]] = {
    "Energy": ["BRENT", "WTI"],
    "Oil & Gas": ["BRENT", "WTI"],
    "Metals": ["COPPER", "GOLD"],
    "Mining": ["COPPER", "GOLD"],
    "Gold": ["GOLD"],
    "FMCG": ["DXY"],
    "IT": ["NASDAQ_FUTURES", "DXY"],
    "Technology": ["NASDAQ_FUTURES"],
    "Pharma": ["DXY"],
    "Banking": ["SPX_FUTURES", "DXY"],
    "Finance": ["SPX_FUTURES"],
    "Auto": ["SPX_FUTURES"],
    "Infrastructure": ["COPPER"],
    "Chemicals": ["COPPER"],
    "Default": ["SPX_FUTURES"],
}


def get_macro_drivers(sector: str | None) -> list[str]:
    """Return the macro instruments relevant to a sector."""
    if sector and sector in SECTOR_MACRO_MAP:
        return SECTOR_MACRO_MAP[sector]
    return SECTOR_MACRO_MAP["Default"]
