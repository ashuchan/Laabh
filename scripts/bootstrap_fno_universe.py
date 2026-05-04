"""Bootstrap the F&O instrument universe from today's NSE F&O bhavcopy.

Every distinct underlying (`tckrsymb`) that appears in the latest F&O UDiFF
bhavcopy is by definition F&O-eligible — that's how NSE itself decides the
F&O list. This script downloads today's bhavcopy (or the most recent
available) and INSERT ... ON CONFLICT DO UPDATE so:

  - Existing instruments get `is_fno=true, is_active=true`.
  - Brand-new instruments are inserted with sensible defaults.
  - Indices listed with options (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
    NIFTYNXT50, SENSEX, BANKEX) are marked is_index=true.

Usage:
    python -m scripts.bootstrap_fno_universe              # uses today's date
    python -m scripts.bootstrap_fno_universe 2026-05-04   # explicit date
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from loguru import logger
from sqlalchemy import text

from src.db import session_scope
from src.dryrun.bhavcopy import BhavcopyMissingError, fetch_fo_bhavcopy


_INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "SENSEX", "BANKEX",
}

_SQL_UPSERT = text("""
    INSERT INTO instruments
        (symbol, exchange, company_name, sector, industry,
         yahoo_symbol, is_fno, is_active, is_index)
    VALUES
        (:symbol, 'NSE', :company_name, 'Unknown', NULL,
         :yahoo_symbol, true, true, :is_index)
    ON CONFLICT (symbol, exchange) DO UPDATE SET
        is_fno    = true,
        is_active = true,
        is_index  = COALESCE(instruments.is_index, EXCLUDED.is_index),
        sector    = COALESCE(instruments.sector, EXCLUDED.sector),
        updated_at = NOW()
    RETURNING symbol, (xmax = 0) AS inserted
""")


async def _resolve_bhavcopy_date(start: date) -> tuple[date, "object"]:
    """Walk back day-by-day until a bhavcopy is found (skips weekends/holidays)."""
    for offset in range(0, 10):
        d = start - timedelta(days=offset)
        try:
            df = await fetch_fo_bhavcopy(d)
            return d, df
        except BhavcopyMissingError:
            logger.debug(f"bootstrap: no bhavcopy for {d} — trying earlier")
            continue
    raise RuntimeError(f"No F&O bhavcopy found within 10 days of {start}")


async def bootstrap(target: date) -> dict[str, int]:
    bhav_date, df = await _resolve_bhavcopy_date(target)
    logger.info(f"bootstrap: using F&O bhavcopy for {bhav_date}")

    if "symbol" not in df.columns:
        raise RuntimeError("Bhavcopy parser produced no 'symbol' column — aborting")

    symbols = sorted({s.strip().upper() for s in df["symbol"].dropna() if s.strip()})
    logger.info(f"bootstrap: {len(symbols)} unique F&O underlyings in bhavcopy")

    inserted = updated = 0
    async with session_scope() as session:
        for sym in symbols:
            is_index = sym in _INDEX_SYMBOLS
            yahoo = None if is_index else f"{sym}.NS"
            res = await session.execute(_SQL_UPSERT, {
                "symbol": sym,
                "company_name": sym,  # placeholder — real name can be filled later
                "yahoo_symbol": yahoo,
                "is_index": is_index,
            })
            row = res.one_or_none()
            if row is None:
                continue
            if row.inserted:
                inserted += 1
            else:
                updated += 1
        await session.commit()

    return {
        "bhavcopy_date": bhav_date.isoformat(),
        "symbols_in_bhavcopy": len(symbols),
        "inserted": inserted,
        "updated": updated,
    }


def main() -> None:
    target = date.today()
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    result = asyncio.run(bootstrap(target))
    print()
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
