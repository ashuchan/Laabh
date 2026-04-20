"""Filter transcript chunks to only those containing financial content."""
from __future__ import annotations

import re

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.instrument import Instrument

# Base financial keywords (English + Hindi)
_BASE_KEYWORDS = {
    "buy", "sell", "target", "stop loss", "stoploss", "bullish", "bearish",
    "nifty", "sensex", "bse", "nse", "breakout", "resistance", "support",
    "rally", "correction", "upside", "downside", "overweight", "underweight",
    "accumulate", "reduce", "hold", "long", "short",
    # Hindi
    "kharido", "beecho", "lakshya", "tejdi", "mandi", "bazaar",
}

_cached_symbols: set[str] | None = None


async def _load_symbols() -> set[str]:
    global _cached_symbols
    if _cached_symbols is not None:
        return _cached_symbols
    async with session_scope() as session:
        result = await session.execute(
            select(Instrument.symbol).where(Instrument.is_active == True)
        )
        symbols = {row[0].upper() for row in result.all()}
    _cached_symbols = symbols
    return symbols


async def contains_financial_content(text: str) -> bool:
    """Return True if the transcript text mentions stocks or financial keywords."""
    upper = text.upper()
    symbols = await _load_symbols()

    # Check base keywords
    for kw in _BASE_KEYWORDS:
        if kw.upper() in upper:
            return True

    # Check stock symbols (word boundary match)
    for sym in symbols:
        if re.search(r"\b" + re.escape(sym) + r"\b", upper):
            return True

    return False


async def filter_chunk(chunk_text: str) -> bool:
    """Return True if chunk should be passed to LLM extraction."""
    result = await contains_financial_content(chunk_text)
    if not result:
        logger.debug("chunk filtered: no financial content")
    return result
