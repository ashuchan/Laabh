"""Startup reconciler for missed daily-critical jobs.

APScheduler's ``misfire_grace_time`` recovers a job whose firing was *queued*
while the scheduler was running but couldn't execute in time. It does NOT
recover a job whose firing time elapsed while the scheduler was completely
offline — that firing is simply lost.

For daily jobs that must run once per trading day (snapshot, EOD tasks,
analyst scoring, daily report, FII/DII), we therefore reconcile at startup:

  1. Determine the most recent expected firing time, weekday-aware.
  2. Look up the latest ``status='completed'`` row in ``job_log`` for that
     scheduler id (rows are written by the ``_logged(...)`` decorator in
     :mod:`src.scheduler`, keyed by APScheduler job id).
  3. If the last success is older than the most recent expected firing,
     schedule a one-shot run ~30 seconds out so the day's work still happens.

The reconciler is intentionally conservative: it never fires the same job
more than once per startup, only fires when the missed scheduled time is
within ``CATCHUP_GRACE_HOURS`` of "now" (a brief-outage recovery window —
not a stale-work reanimator), and always defers to a regular cron firing
if "now" is already before the most recent expected time.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from loguru import logger
from pytz import timezone as tz
from sqlalchemy import text

from src.config import get_settings
from src.db import session_scope


# Map: APScheduler job id -> (hour, minute) of the latest expected firing.
# Order matches the cron jobs declared in src.scheduler.build_scheduler().
#
# Only jobs that *must run exactly once per trading day* belong here. Jobs
# that fire many times a day (intraday actions, interval pollers) recover
# naturally on the next firing and don't need explicit catch-up.
DAILY_CRITICAL: dict[str, tuple[int, int]] = {
    "equity_morning_allocation": (9, 10),
    "daily_snapshot": (15, 35),
    "fno_eod": (15, 40),
    "equity_eod_squareoff": (15, 20),
    "yahoo_eod": (18, 0),
    "update_analyst_scores": (18, 0),
    "fno_fii_dii": (18, 0),
    "daily_report": (18, 30),
    "fno_issue_review_loop": (18, 30),
}

# Only catch up firings whose scheduled time was within this window. The
# reconciler's purpose is to recover from a *brief* outage that overlapped a
# scheduled firing -- NOT to re-run yesterday's morning allocation at 3 AM
# tonight. If the system was down for hours through a daily firing, the
# right answer is to skip and let the next regular cron firing take over;
# the catch-up would only generate stale work (running daily_report against
# yesterday's portfolio at noon today, etc.).
#
# Match this to the scheduler-wide ``misfire_grace_time`` plus a small
# buffer so the two layers behave consistently.
CATCHUP_GRACE_HOURS = 2

# How long to wait before the catch-up run, so the scheduler is fully booted
# and other startup tasks have had a moment to settle.
CATCHUP_DELAY_SECONDS = 30


def _last_expected_firing(now_local: datetime, hour: int, minute: int) -> datetime:
    """Return the most recent weekday (Mon–Fri) firing of (hour, minute) at-or-before now."""
    target_today = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate = target_today if target_today <= now_local else target_today - timedelta(days=1)
    while candidate.weekday() >= 5:  # Saturday=5, Sunday=6
        candidate -= timedelta(days=1)
    return candidate


async def _last_success_at(job_name: str) -> datetime | None:
    """Return the timestamp of the most recent ``completed`` row in ``job_log``."""
    async with session_scope() as session:
        result = await session.execute(
            text(
                "SELECT MAX(created_at) FROM job_log "
                "WHERE job_name = :name AND status = 'completed'"
            ),
            {"name": job_name},
        )
        return result.scalar_one_or_none()


def _resolve_job_func(sched: AsyncIOScheduler, job_id: str) -> Callable[[], Awaitable[None]] | None:
    """Look up the coroutine bound to ``job_id`` from the live scheduler."""
    job = sched.get_job(job_id)
    if job is None:
        logger.warning(f"reconciler: job {job_id} not registered, skipping")
        return None
    return job.func


async def reconcile_missed(
    sched: AsyncIOScheduler,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> int:
    """Schedule one-shot catch-up runs for any DAILY_CRITICAL job that missed today.

    Returns the number of catch-up jobs scheduled. Idempotent within a single
    process: each catch-up uses a unique job id derived from the current
    timestamp, so multiple invocations within the same second would collide
    and the second call would be replaced.

    :param as_of: Override "now" for testing / dry-run. Defaults to the live
        wall-clock time in the configured timezone. Per CLAUDE.md convention:
        live behavior is unchanged when omitted.
    :param dryrun_run_id: When set, the reconciler logs but does NOT actually
        schedule catch-up jobs — used by replay tooling to inspect what would
        have happened without mutating the live scheduler.
    """
    settings = get_settings()
    local = tz(settings.timezone)
    now_local = as_of.astimezone(local) if as_of is not None else datetime.now(local)
    earliest_acceptable = now_local - timedelta(hours=CATCHUP_GRACE_HOURS)
    dryrun = dryrun_run_id is not None

    scheduled = 0
    for job_id, (hour, minute) in DAILY_CRITICAL.items():
        last_due = _last_expected_firing(now_local, hour, minute)
        # Tight grace window: only catch up if the scheduler missed THIS job's
        # firing within the last CATCHUP_GRACE_HOURS. Anything older is stale
        # work and the next cron firing should be left to handle it.
        if last_due < earliest_acceptable:
            continue

        last_success = await _last_success_at(job_id)
        # job_log.created_at is timestamptz so it's already tz-aware.
        if last_success is not None and last_success >= last_due:
            continue

        func = _resolve_job_func(sched, job_id)
        if func is None:
            continue

        run_at = now_local + timedelta(seconds=CATCHUP_DELAY_SECONDS)
        catchup_id = f"catchup_{job_id}_{int(now_local.timestamp())}"

        if dryrun:
            scheduled += 1
            logger.info(
                f"reconciler[dryrun={dryrun_run_id}]: would catch-up {job_id} "
                f"(expected={last_due.isoformat()}, last_success={last_success})"
            )
            continue

        try:
            sched.add_job(
                func,
                DateTrigger(run_date=run_at),
                id=catchup_id,
                misfire_grace_time=300,
                coalesce=True,
                max_instances=1,
            )
            scheduled += 1
            logger.info(
                f"reconciler: catch-up scheduled for {job_id} "
                f"(expected={last_due.isoformat()}, last_success={last_success}, "
                f"running_at={run_at.isoformat()})"
            )
        except Exception as exc:
            logger.error(f"reconciler: failed to schedule {job_id}: {exc!r}")

    if scheduled == 0:
        logger.info("reconciler: no daily-critical jobs need catch-up")
    return scheduled


__all__ = ["reconcile_missed", "DAILY_CRITICAL"]
