#!/usr/bin/env python3
"""Backfill price_daily.prev_close and price_daily.change_pct.

`prev_close` and `change_pct` are NULL across most rows because the daily
ingestion writes only OHLC + volume. The two columns are downstream
derivatives — we can compute them in-place by joining each row to the
previous row for the same instrument.

Read-only-by-default unless --apply is passed.

Usage:
    python scripts/backfill_price_daily_changes.py            # dry-run summary
    python scripts/backfill_price_daily_changes.py --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text


SQL_DRY_RUN = """
WITH paired AS (
    SELECT
        pd.instrument_id, pd.date, pd.close,
        LAG(pd.close) OVER (PARTITION BY pd.instrument_id ORDER BY pd.date) AS lag_close,
        pd.prev_close, pd.change_pct
    FROM price_daily pd
)
SELECT
    COUNT(*) FILTER (WHERE prev_close IS NULL AND lag_close IS NOT NULL) AS prev_close_to_fill,
    COUNT(*) FILTER (WHERE change_pct IS NULL AND lag_close IS NOT NULL AND lag_close <> 0) AS change_pct_to_fill,
    COUNT(*) FILTER (WHERE lag_close IS NULL) AS first_row_per_instrument,
    COUNT(*) AS total_rows
FROM paired;
"""

SQL_APPLY = """
WITH paired AS (
    SELECT
        pd.instrument_id, pd.date,
        LAG(pd.close) OVER (PARTITION BY pd.instrument_id ORDER BY pd.date) AS lag_close
    FROM price_daily pd
)
UPDATE price_daily AS target
SET
    prev_close = paired.lag_close,
    change_pct = ROUND(((target.close - paired.lag_close) / paired.lag_close * 100)::numeric, 4)
FROM paired
WHERE target.instrument_id = paired.instrument_id
  AND target.date = paired.date
  AND paired.lag_close IS NOT NULL
  AND paired.lag_close <> 0
  AND (target.prev_close IS NULL OR target.change_pct IS NULL);
"""


async def main(apply: bool) -> None:
    from src.db import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        summary = (await db.execute(text(SQL_DRY_RUN))).fetchone()
        print(f"price_daily total rows: {summary[3]:,}")
        print(f"  first row per instrument (no prior close): {summary[2]:,}")
        print(f"  rows with NULL prev_close that will fill:  {summary[0]:,}")
        print(f"  rows with NULL change_pct that will fill:  {summary[1]:,}")

        if not apply:
            print("\nDRY-RUN — no rows updated. Pass --apply to commit.")
            return

        print("\nApplying...")
        result = await db.execute(text(SQL_APPLY))
        await db.commit()
        print(f"Updated {result.rowcount:,} row(s).")

        # Verify
        verify = (await db.execute(text(
            "SELECT COUNT(*) FILTER (WHERE change_pct IS NOT NULL) AS done, "
            "COUNT(*) AS total FROM price_daily"
        ))).fetchone()
        print(f"After apply: {verify[0]:,} of {verify[1]:,} rows have change_pct.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit the backfill.")
    args = p.parse_args()
    asyncio.run(main(args.apply))
