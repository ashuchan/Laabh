"""Backfill `sector` and `industry` from yfinance for instruments missing them.

Targets rows where sector is NULL or 'Unknown'. yfinance's `info["sector"]`
and `info["industry"]` use a coarse GICS-ish 11-sector taxonomy that's
exactly the resolution Phase 2's `score_macro` and the policy fan-out need.

Idempotent — re-runs skip already-classified rows. Indices (`is_index=true`)
are skipped because they don't map to a sector cleanly.

Usage:
    python -m scripts.backfill_sectors
    python -m scripts.backfill_sectors --force   # also re-classify rows that
                                                 # already have a sector
"""
from __future__ import annotations

import asyncio
import sys
import time

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from src.db import session_scope
from src.models.instrument import Instrument


# Yahoo's coarse sector → keep this list small so the values match what
# Phase 2 / sector fan-out / instruments table expect.
_YAHOO_TO_CANONICAL = {
    "Financial Services": "Financials",
    "Technology": "IT",
    "Healthcare": "Healthcare",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "FMCG",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Basic Materials": "Materials",
    "Utilities": "Utilities",
    "Communication Services": "Telecom",
    "Real Estate": "Realty",
}


def _normalise(yahoo_sector: str | None) -> str | None:
    if not yahoo_sector:
        return None
    return _YAHOO_TO_CANONICAL.get(yahoo_sector, yahoo_sector)


def _yf_lookup(yahoo_symbol: str) -> tuple[str | None, str | None]:
    """Return (sector, industry) from yfinance, or (None, None) on miss."""
    import yfinance as yf
    try:
        info = yf.Ticker(yahoo_symbol).info
        if not isinstance(info, dict):
            return None, None
        return info.get("sector"), info.get("industry")
    except Exception as exc:
        logger.debug(f"yfinance lookup failed for {yahoo_symbol}: {exc}")
        return None, None


async def backfill(force: bool = False) -> dict[str, int]:
    async with session_scope() as session:
        q = select(Instrument).where(Instrument.is_index == False)  # noqa: E712
        if not force:
            from sqlalchemy import or_
            q = q.where(or_(
                Instrument.sector.is_(None),
                Instrument.sector == "Unknown",
            ))
        result = await session.execute(q)
        rows = list(result.scalars())

    logger.info(f"backfill_sectors: {len(rows)} rows to classify")

    updated = skipped = failed = 0
    for inst in rows:
        ysym = inst.yahoo_symbol or f"{inst.symbol}.NS"
        sector_raw, industry = _yf_lookup(ysym)
        canonical = _normalise(sector_raw)
        if not canonical:
            failed += 1
            logger.debug(f"  [skip] {inst.symbol} ({ysym}): no sector from yfinance")
            continue

        try:
            async with session_scope() as session:
                await session.execute(
                    update(Instrument)
                    .where(Instrument.id == inst.id)
                    .values(sector=canonical, industry=industry or inst.industry)
                )
            updated += 1
            logger.info(f"  [ok]   {inst.symbol:14s} -> {canonical}  ({industry or 'n/a'})")
        except SQLAlchemyError as exc:
            failed += 1
            logger.warning(f"  [fail] {inst.symbol}: DB error {exc}")

        # Polite to yfinance — avoid rate limit
        time.sleep(0.15)

    return {"considered": len(rows), "updated": updated, "skipped": skipped, "failed": failed}


def main() -> None:
    force = "--force" in sys.argv[1:]
    result = asyncio.run(backfill(force=force))
    print()
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
