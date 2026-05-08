#!/usr/bin/env python3
"""Seed the analysts table from analyst_name_raw observed in signals.

Each distinct non-null analyst_name_raw becomes one analysts row, with
hit-rate / signals stats computed from resolved signals where available.

Usage:
    python scripts/seed_analysts_from_signals.py            # dry-run
    python scripts/seed_analysts_from_signals.py --apply    # commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text


SQL_AGGREGATE = """
SELECT
    analyst_name_raw,
    COUNT(*) AS total_signals,
    COUNT(*) FILTER (WHERE outcome_pnl_pct > 0) AS hit_target,
    COUNT(*) FILTER (WHERE outcome_pnl_pct < 0) AS hit_sl,
    AVG(outcome_pnl_pct) FILTER (WHERE outcome_pnl_pct IS NOT NULL) AS avg_return_pct
FROM signals
WHERE analyst_name_raw IS NOT NULL AND TRIM(analyst_name_raw) <> ''
GROUP BY analyst_name_raw
ORDER BY total_signals DESC;
"""

SQL_INSERT = """
INSERT INTO analysts (
    name, organization, designation,
    total_signals, signals_hit_target, signals_hit_sl,
    hit_rate, avg_return_pct, credibility_score, created_at, updated_at
) VALUES (
    :name, :organization, :designation,
    :total, :hit, :sl, :hit_rate, :avg_ret, :cred, NOW(), NOW()
)
ON CONFLICT (name) DO UPDATE SET
    total_signals = EXCLUDED.total_signals,
    signals_hit_target = EXCLUDED.signals_hit_target,
    signals_hit_sl = EXCLUDED.signals_hit_sl,
    hit_rate = EXCLUDED.hit_rate,
    avg_return_pct = EXCLUDED.avg_return_pct,
    credibility_score = EXCLUDED.credibility_score,
    updated_at = NOW();
"""


def _split_name(raw: str) -> tuple[str, str | None, str | None]:
    """Heuristic: 'Name @ Org' / 'Name (Org)' / 'Name - Designation'."""
    s = raw.strip()
    org = None
    designation = None
    for sep in (" @ ", " - ", " | "):
        if sep in s:
            head, tail = s.split(sep, 1)
            return head.strip(), tail.strip(), None
    if "(" in s and s.endswith(")"):
        head, tail = s[:-1].split("(", 1)
        return head.strip(), tail.strip(), None
    return s, org, designation


async def main(apply: bool) -> None:
    from src.db import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        rows = (await db.execute(text(SQL_AGGREGATE))).fetchall()
        print(f"Distinct analyst_name_raw values found: {len(rows)}")
        print(f"Top 10 by total_signals:")
        for r in rows[:10]:
            print(f"  {r[0]:<60} signals={r[1]:>4}  pnl_avg={r[4]}")

        if not apply:
            print("\nDRY-RUN — no inserts. Pass --apply to commit.")
            return

        print("\nApplying...")
        inserted = 0
        for r in rows:
            name, org, des = _split_name(r[0])
            total = int(r[1])
            hit = int(r[2] or 0)
            sl = int(r[3] or 0)
            avg_ret = float(r[4]) if r[4] is not None else None
            resolved = hit + sl
            hit_rate = (hit / resolved) if resolved else None
            cred = max(0.2, min(0.9, hit_rate or 0.5))
            await db.execute(text(SQL_INSERT), {
                "name": name, "organization": org, "designation": des,
                "total": total, "hit": hit, "sl": sl,
                "hit_rate": hit_rate, "avg_ret": avg_ret, "cred": cred,
            })
            inserted += 1
        await db.commit()
        print(f"Upserted {inserted} analyst rows.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    asyncio.run(main(p.parse_args().apply))
