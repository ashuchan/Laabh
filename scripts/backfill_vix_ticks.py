#!/usr/bin/env python3
"""Backfill vix_ticks from yfinance ^INDIAVIX history.

Replaces / extends recent rows in vix_ticks. The VIX collector hasn't run
since 2026-05-04; this fills the gap.

Usage:
    python scripts/backfill_vix_ticks.py            # dry-run summary
    python scripts/backfill_vix_ticks.py --apply    # write
    python scripts/backfill_vix_ticks.py --apply --days 60
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text


def _classify(vix: float) -> str:
    if vix < 12:
        return "low"
    if vix > 18:
        return "high"
    return "neutral"


def _fetch_yf(days: int):
    import yfinance as yf
    t = yf.Ticker("^INDIAVIX")
    hist = t.history(period=f"{days}d", interval="1d")
    if hist.empty:
        return []
    rows = []
    for ts, row in hist.iterrows():
        close = float(row["Close"]) if not (row["Close"] != row["Close"]) else None
        if close is None:
            continue
        when = ts.to_pydatetime()
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        else:
            when = when.astimezone(timezone.utc)
        rows.append((when, close))
    return rows


async def main(apply: bool, days: int) -> None:
    from src.db import get_session_factory

    yf_rows = _fetch_yf(days)
    print(f"yfinance ^INDIAVIX returned {len(yf_rows)} rows over the last {days} days.")
    if not yf_rows:
        return
    print(f"  earliest: {yf_rows[0][0].date()}  vix={yf_rows[0][1]:.2f}")
    print(f"  latest:   {yf_rows[-1][0].date()}  vix={yf_rows[-1][1]:.2f}")

    if not apply:
        print("\nDRY-RUN — no inserts. Pass --apply to commit.")
        return

    factory = get_session_factory()
    inserted = 0
    async with factory() as db:
        for when, close in yf_rows:
            await db.execute(
                text("""
                    INSERT INTO vix_ticks (timestamp, vix_value, regime)
                    VALUES (:ts, :v, :r)
                    ON CONFLICT (timestamp) DO UPDATE SET
                        vix_value = EXCLUDED.vix_value,
                        regime    = EXCLUDED.regime
                """),
                {"ts": when, "v": close, "r": _classify(close)},
            )
            inserted += 1
        await db.commit()
    print(f"Upserted {inserted} vix_ticks rows.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    asyncio.run(main(args.apply, args.days))
