"""Reads top-N from fno_candidates and returns a frozen universe for the day."""
from __future__ import annotations

import uuid
from datetime import date

from loguru import logger
from sqlalchemy import select, desc

from src.config import get_settings
from src.db import session_scope
from src.models.fno_candidate import FNOCandidate
from src.models.instrument import Instrument


async def load_universe(
    trading_date: date,
    *,
    as_of=None,
    dryrun_run_id=None,
) -> list[dict]:
    """Return top-N candidates from fno_candidates for *trading_date*.

    Returns list of dicts: [{id, symbol, name}] ordered by composite_score desc.
    """
    settings = get_settings()
    cap = settings.laabh_quant_universe_size_cap

    async with session_scope() as session:
        q = (
            select(FNOCandidate, Instrument)
            .join(Instrument, Instrument.id == FNOCandidate.underlying_id)
            .where(FNOCandidate.date == trading_date)
            .order_by(desc(FNOCandidate.composite_score))
            .limit(cap)
        )
        rows = (await session.execute(q)).all()

    result = []
    for candidate, instrument in rows:
        result.append({
            "id": instrument.id,
            "symbol": instrument.symbol,
            "name": instrument.name,
        })

    logger.info(f"universe: loaded {len(result)} underlyings for {trading_date}")
    return result
