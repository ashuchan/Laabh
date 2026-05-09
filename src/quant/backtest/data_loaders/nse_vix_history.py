"""Backfill ``vix_ticks`` over a date range using yfinance for historical VIX.

Wraps ``src.fno.vix_collector.run_once(as_of=...)`` which already handles:
  * Historical fetch via yfinance (^INDIAVIX)
  * Idempotent insert via PG ``on_conflict_do_nothing(timestamp)``
  * Regime classification

We loop over trading days, fetching one VIX reading per day at 15:30 IST
(close-of-day). Intraday-resolution VIX is not free historically; daily
close is what live-shadow code already falls back to and is sufficient
for backtest VIX reads.
"""
from __future__ import annotations

import time as _time
import uuid
from datetime import date, datetime, time
from typing import Iterable

import pytz
from loguru import logger

from src.fno.vix_collector import run_once
from src.quant.backtest.clock import trading_days_between


_IST = pytz.timezone("Asia/Kolkata")


async def backfill(
    start_date: date,
    end_date: date,
    *,
    holidays: Iterable[date] = (),
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Backfill ``vix_ticks`` for every trading day in ``[start, end]``.

    One row inserted per day at 15:30 IST. Existing rows are skipped via
    PG conflict resolution.

    Returns ``{"days": N, "fetched": M, "failed": K}``. ``fetched`` counts
    successful API calls; rows that already existed still count as "fetched"
    because the upstream fetcher doesn't distinguish.
    """
    days = trading_days_between(start_date, end_date, holidays=holidays)
    logger.info(
        f"nse_vix_history: backfilling {len(days)} trading days "
        f"in {start_date}..{end_date}"
    )
    t0 = _time.monotonic()

    fetched = 0
    failed = 0
    for d in days:
        # 15:30 IST close-of-day
        as_of_ist = _IST.localize(datetime.combine(d, time(15, 30)))
        try:
            # vix_collector.run_once already accepts dryrun_run_id and
            # propagates to vix_ticks.dryrun_run_id, so this convention
            # parameter is genuinely usable here.
            await run_once(as_of=as_of_ist, dryrun_run_id=dryrun_run_id)
            fetched += 1
        except Exception as exc:
            # Holidays, network blips, weekends in the calendar that the
            # upstream calendar didn't catch — log and continue.
            logger.warning(f"nse_vix_history: {d} failed: {exc!r}")
            failed += 1

    duration = _time.monotonic() - t0
    logger.info(
        f"nse_vix_history: done. days={len(days)} fetched={fetched} "
        f"failed={failed} duration_sec={duration:.2f}"
    )
    return {"days": len(days), "fetched": fetched, "failed": failed}
