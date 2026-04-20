"""Match extracted stock symbols / names → instrument IDs."""
from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.instrument import Instrument

# Common aliases used in Indian financial media
ALIASES: dict[str, str] = {
    "RIL": "RELIANCE",
    "RELIANCE INDUSTRIES": "RELIANCE",
    "INFY": "INFY",
    "INFOSYS": "INFY",
    "TAMO": "TATAMOTORS",
    "BAJAJ FIN": "BAJFINANCE",
    "BAJAJ FINANCE": "BAJFINANCE",
    "SBI": "SBIN",
    "L&T": "LT",
    "LARSEN": "LT",
    "M&M": "M&M",
    "MAHINDRA": "M&M",
    "HDFC TWINS": "HDFCBANK",
    "HUL": "HINDUNILVR",
}


class EntityMatcher:
    """Resolve noisy stock references to `instruments` rows."""

    def __init__(self) -> None:
        self._cache: dict[str, uuid.UUID | None] = {}

    async def match(self, session: AsyncSession, symbol_or_name: str) -> uuid.UUID | None:
        """Return instrument_id for a raw symbol or company name, or None."""
        if not symbol_or_name:
            return None
        key = symbol_or_name.strip().upper()
        if key in self._cache:
            return self._cache[key]

        canonical = ALIASES.get(key, key)

        # Exact symbol match
        row = await session.execute(
            select(Instrument.id).where(
                Instrument.symbol == canonical,
                Instrument.is_active == True,  # noqa: E712
            )
        )
        inst_id = row.scalar_one_or_none()

        # Fuzzy company_name match via pg_trgm
        if inst_id is None:
            row = await session.execute(
                select(Instrument.id)
                .where(or_(
                    func.upper(Instrument.company_name).like(f"%{canonical}%"),
                    func.upper(Instrument.symbol) == canonical,
                ))
                .order_by(func.length(Instrument.company_name))
                .limit(1)
            )
            inst_id = row.scalar_one_or_none()

        if inst_id is None:
            logger.debug(f"Could not match entity: {symbol_or_name!r}")
        self._cache[key] = inst_id
        return inst_id
