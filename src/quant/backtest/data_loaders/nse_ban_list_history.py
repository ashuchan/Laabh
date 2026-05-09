"""Backfill ``fno_ban_list`` from NSE archives over a date range.

Wraps the existing live-mode ``src.fno.ban_list.fetch_today`` in a date-range
loop. The wrapped function is already idempotent (skips rows that exist),
handles 404 (holiday/weekend), and logs sane progress. We just enumerate
business days.

Decision Note (no parallelism here):
  * NSE rate-limits aggressively under unauthenticated download. The wrapped
    fetcher uses a 5-attempt exponential backoff per date. Sequential
    iteration with that backoff is more reliable than concurrent calls.
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime
from typing import Iterable

from loguru import logger

from src.fno.ban_list import fetch_today
from src.quant.backtest.clock import trading_days_between


async def backfill(
    start_date: date,
    end_date: date,
    *,
    holidays: Iterable[date] = (),
    source: str = "NSE",
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Backfill ``fno_ban_list`` for every trading day in ``[start, end]``.

    Args:
        start_date: First date (inclusive).
        end_date: Last date (inclusive).
        holidays: NSE holiday set; skipped during enumeration.
        source: Source-tag stored on each row (default "NSE").
        as_of: CLAUDE.md convention parameter — accepted but unused here
            because the loader is a bulk-historical operation (the dates it
            writes are determined by ``start_date``/``end_date``, not by
            "now"). Default keeps behavior unchanged.
        dryrun_run_id: CLAUDE.md convention parameter — accepted but not
            propagated. ``fetch_today`` (the upstream live-mode wrapper) does
            not currently expose a dryrun argument; the underlying
            ``fno_ban_list`` model has the column but the live writer doesn't
            populate it. Threading dry-run through here is a no-op until that
            shim is added — flagged for follow-up.

    Returns:
        ``{"days": N, "inserted": M, "skipped_404": K}``. ``inserted`` counts
        new rows (already-stored rows are skipped by ``fetch_today`` itself).
    """
    days = trading_days_between(start_date, end_date, holidays=holidays)
    logger.info(
        f"nse_ban_list_history: backfilling {len(days)} trading days "
        f"in {start_date}..{end_date}"
    )
    t0 = time.monotonic()

    inserted_total = 0
    skipped_404 = 0
    for d in days:
        try:
            inserted = await fetch_today(ban_date=d, source=source)
            inserted_total += inserted
        except Exception as exc:
            # fetch_today swallows 404; anything else here is a real error.
            # Log and continue so a single bad day doesn't kill the whole
            # backfill.
            logger.warning(f"nse_ban_list_history: {d} failed: {exc!r}")
            skipped_404 += 1

    duration = time.monotonic() - t0
    logger.info(
        f"nse_ban_list_history: done. days={len(days)} "
        f"inserted={inserted_total} failed={skipped_404} "
        f"duration_sec={duration:.2f}"
    )
    return {
        "days": len(days),
        "inserted": inserted_total,
        "skipped_404": skipped_404,
    }
