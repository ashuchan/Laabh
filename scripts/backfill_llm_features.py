"""Replay Phase 3 v10 prompts against historical dates under a batch UUID.

Plan reference: docs/llm_feature_generator/backfill_plan.md §4 Phase B.

For each historical trading day with Phase 2 candidates in fno_candidates,
this script:
  * picks all phase=2 candidates for D,
  * for each candidate, calls ``run_v10_backfill_one_candidate`` which
    builds the v10 prompt with ``news_cutoff=ist(D, 9, 0)`` and persists
    a row to ``llm_decision_log`` keyed by ``dryrun_run_id=batch_uuid``,
  * after every date is processed, attributes outcomes via
    ``attribute_llm_outcomes(dryrun_run_id=batch_uuid)``.

Idempotent and resumable on ``(run_date, candidate_id, batch_uuid)`` —
``run_v10_backfill_one_candidate`` itself checks for an existing row
before calling Claude, so a re-run skips completed candidates.

Rate-limit: token-bucket sized to Anthropic tier 1's 50 req/min ceiling,
configurable via ``--rate-limit-per-min``.

Cost cap: ``--max-cost USD`` halts the script before exceeding the
budget. Sonnet-4 pricing (input $3/Mtok, output $15/Mtok) is used to
estimate cumulative spend; the cap is approximate to within ~5%.

Holdout sentinel: the most-recent ``--holdout-tail-days`` are still
backfilled (so the bootstrap model can be scored against them) but the
range is recorded in ``backfill_holdout_sentinels`` so the calibration
script knows which dates to exclude from the FIT set.

Usage::

    python scripts/backfill_llm_features.py --days 30 --batch-id MoneyRatnam_backfill_v1
    python scripts/backfill_llm_features.py --days 180 --batch-id MoneyRatnam_backfill_v1 --max-cost 50
    python scripts/backfill_llm_features.py --days 30 --batch-id MoneyRatnam_backfill_v1 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, datetime, time, timedelta
from typing import Iterable

import pytz
from loguru import logger
from sqlalchemy import select, text

from src.db import dispose_engine, session_scope
from src.dryrun.side_effects import set_dryrun_run_id
from src.fno.backfill_batch import DEFAULT_HOLDOUT_TAIL_DAYS, batch_label_to_uuid
from src.models.fno_candidate import FNOCandidate
from src.quant.backtest.clock import trading_days_between
from src.utils.rate_limit import TokenBucketRateLimiter

_IST = pytz.timezone("Asia/Kolkata")

# Anthropic pricing (USD per million tokens). Sonnet 4 = the live model
# (see Settings.fno_phase3_llm_model). Update if you switch model.
#
# Known systematic skew: the v10 system prompt has prompt caching enabled
# (cache_control: ephemeral). Cached input tokens cost ~$0.30/Mtok rather
# than $3/Mtok, so after the first call seeds the cache the real spend
# will be 20-40% below this estimate. The skew is in the SAFE direction
# for --max-cost (we halt sooner than necessary) but the reported
# cumulative cost will overstate actual Anthropic billing. To get the
# exact figure, read `cache_read_input_tokens` off the message response
# and price it at $0.30/Mtok — left as a future enhancement; the
# bootstrap budget is ~$50 so the precision isn't load-bearing.
_USD_PER_MTOK_INPUT = 3.0
_USD_PER_MTOK_OUTPUT = 15.0

# Anthropic tier-1 ceiling is 50 req/min. Default to 80% of that.
_DEFAULT_RATE_LIMIT_PER_MIN = int(os.getenv("BACKFILL_LLM_RATE_LIMIT_PER_MIN", "45"))


def _ist_morning(d: date) -> datetime:
    """09:00 IST on date ``d``, in UTC. The canonical news-cutoff anchor."""
    return _IST.localize(datetime.combine(d, time(9, 0))).astimezone(pytz.UTC)


def _estimate_cost_usd(tokens_in: int, tokens_out: int) -> float:
    return (
        tokens_in * _USD_PER_MTOK_INPUT / 1_000_000.0
        + tokens_out * _USD_PER_MTOK_OUTPUT / 1_000_000.0
    )


async def _record_holdout_sentinel(
    *,
    batch_uuid,
    batch_label: str,
    holdout_start: date,
    holdout_end: date,
) -> None:
    """Upsert the holdout window for this batch so the calibration script
    reads the exact same window the backfill recorded."""
    async with session_scope() as session:
        await session.execute(text("""
            INSERT INTO backfill_holdout_sentinels
                (batch_uuid, batch_label, holdout_start_date, holdout_end_date)
            VALUES (:u, :l, :s, :e)
            ON CONFLICT (batch_uuid) DO UPDATE SET
                batch_label = EXCLUDED.batch_label,
                holdout_start_date = EXCLUDED.holdout_start_date,
                holdout_end_date = EXCLUDED.holdout_end_date
        """), {
            "u": str(batch_uuid),
            "l": batch_label,
            "s": holdout_start,
            "e": holdout_end,
        })


async def _phase2_candidates_for(d: date) -> list[str]:
    """Return the instrument_ids that passed Phase 2 on date ``d``.

    Reads from ``fno_candidates`` (phase=2) which the prereqs script
    populated for the historical window. Live scope only (no
    dryrun_run_id) — backfill candidates are themselves live-scope
    phase=2 rows; only the LLM replay output gets the batch UUID.
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(FNOCandidate.instrument_id).where(
                FNOCandidate.run_date == d,
                FNOCandidate.phase == 2,
                FNOCandidate.dryrun_run_id.is_(None),
            )
        )).all()
    return [str(r.instrument_id) for r in rows]


async def _has_v10_row(d: date, instrument_id: str, batch_uuid) -> bool:
    """Idempotent guard: skip the LLM call if a v10 row for
    (D, instrument, batch_uuid) is already present."""
    async with session_scope() as session:
        row = (await session.execute(text("""
            SELECT 1
            FROM llm_decision_log
            WHERE run_date = :d
              AND instrument_id = :i
              AND phase = 'fno_thesis'
              AND prompt_version = 'v10_continuous'
              AND dryrun_run_id = :u
            LIMIT 1
        """), {"d": d, "i": instrument_id, "u": str(batch_uuid)})).first()
    return row is not None


async def _process_one_date(
    d: date,
    *,
    batch_uuid,
    rate_limiter: TokenBucketRateLimiter,
    cost_tracker: dict,
    max_cost: float | None,
    dry_run: bool,
) -> dict[str, int]:
    """Backfill v10 features for every Phase 2 candidate on date ``d``.

    Returns counts: {written, skipped, failed}.
    """
    candidates = await _phase2_candidates_for(d)
    if not candidates:
        logger.warning(f"backfill_llm: {d} has no Phase 2 candidates — skipping")
        return {"written": 0, "skipped": 0, "failed": 0}

    as_of = _ist_morning(d)
    news_cutoff = as_of  # plan §3.2: news_cutoff = as_of for backfill

    n_arms = max(len(candidates), 1)
    propensity = 1.0 / n_arms

    written = skipped = failed = 0
    from src.fno.thesis_synthesizer import run_v10_backfill_one_candidate

    # Pull the regime snapshot for the day once and reuse across candidates
    # — the regime block is identical per (D, prompt). Best-effort.
    market_regime = None
    try:
        from src.fno.regime_classifier import get_latest_regime
        market_regime = await get_latest_regime(d)
    except Exception as exc:
        logger.debug(f"backfill_llm: regime fetch failed for {d}: {exc!r}")

    for inst_id in candidates:
        if max_cost is not None and cost_tracker["cumulative_usd"] >= max_cost:
            logger.warning(
                f"backfill_llm: cost cap ${max_cost:.2f} reached "
                f"(actual ${cost_tracker['cumulative_usd']:.2f}) — halting"
            )
            return {"written": written, "skipped": skipped, "failed": failed,
                    "halted_on_cost": True}

        if await _has_v10_row(d, inst_id, batch_uuid):
            skipped += 1
            continue

        if dry_run:
            logger.info(f"backfill_llm: [dry-run] would call v10 for {d} / {inst_id}")
            skipped += 1
            continue

        await rate_limiter.acquire()
        try:
            with set_dryrun_run_id(batch_uuid):
                stats = await run_v10_backfill_one_candidate(
                    candidate_id=inst_id,
                    run_date=d,
                    as_of=as_of,
                    dryrun_run_id=batch_uuid,
                    news_cutoff=news_cutoff,
                    bandit_arm_propensity=propensity,
                    propensity_source="imputed",
                    market_regime=market_regime,
                )
            if stats.get("wrote_row"):
                written += 1
                cost = _estimate_cost_usd(stats["tokens_in"], stats["tokens_out"])
                cost_tracker["cumulative_usd"] += cost
                cost_tracker["cumulative_tokens_in"] += stats["tokens_in"]
                cost_tracker["cumulative_tokens_out"] += stats["tokens_out"]
                logger.debug(
                    f"backfill_llm: {d} {inst_id} ✓ "
                    f"in={stats['tokens_in']} out={stats['tokens_out']} "
                    f"cost=${cost:.4f} cum=${cost_tracker['cumulative_usd']:.2f}"
                )
            else:
                # Row already existed (race-with-self) or candidate context missing.
                skipped += 1
        except Exception as exc:
            logger.warning(f"backfill_llm: {d} {inst_id} failed: {exc!r}")
            failed += 1

    return {"written": written, "skipped": skipped, "failed": failed}


async def main(
    *,
    days: int,
    from_date: date | None,
    to_date: date | None,
    batch_label: str,
    holdout_tail_days: int,
    rate_limit_per_min: int,
    max_cost: float | None,
    dry_run: bool,
    holidays: Iterable[date] | None = None,
) -> int:
    batch_uuid = batch_label_to_uuid(batch_label)
    logger.info(
        f"backfill_llm: batch_label={batch_label!r} → batch_uuid={batch_uuid}"
    )

    end = to_date or (date.today() - timedelta(days=1))
    start = from_date or (end - timedelta(days=int(days * 1.5)))

    # Holidays default: read from database/nse_holidays.json. Missing
    # holidays surface as runtime errors when bhavcopy 404s on that date,
    # so populating the file before launching is recommended.
    if holidays is None:
        from src.fno.nse_holidays import load_nse_holidays
        holidays = load_nse_holidays(start, end)

    trading_days = trading_days_between(start, end, holidays=holidays)
    if from_date is None:
        trading_days = trading_days[-days:]

    if not trading_days:
        logger.warning(f"backfill_llm: no trading days between {start} and {end}")
        return 0

    # Holdout = the most recent K trading days. The earlier (len - K) days
    # are the FIT set; the K-day tail is the held-out window for true
    # out-of-sample scoring. We still backfill the tail so we can score it.
    if holdout_tail_days <= 0 or holdout_tail_days >= len(trading_days):
        logger.warning(
            f"backfill_llm: --holdout-tail-days={holdout_tail_days} invalid for "
            f"{len(trading_days)} trading days — skipping holdout sentinel"
        )
        holdout_start = holdout_end = None
    else:
        holdout_start = trading_days[-holdout_tail_days]
        holdout_end = trading_days[-1]
        await _record_holdout_sentinel(
            batch_uuid=batch_uuid,
            batch_label=batch_label,
            holdout_start=holdout_start,
            holdout_end=holdout_end,
        )
        logger.info(
            f"backfill_llm: holdout window recorded — "
            f"{holdout_start} → {holdout_end} ({holdout_tail_days} days)"
        )

    rate_limiter = TokenBucketRateLimiter(rate_limit_per_min)
    cost_tracker = {
        "cumulative_usd": 0.0,
        "cumulative_tokens_in": 0,
        "cumulative_tokens_out": 0,
    }

    total_written = total_skipped = total_failed = 0
    halted = False
    for idx, d in enumerate(trading_days, start=1):
        logger.info(
            f"backfill_llm: [{idx}/{len(trading_days)}] {d} "
            f"(cum_cost=${cost_tracker['cumulative_usd']:.2f})"
        )
        res = await _process_one_date(
            d,
            batch_uuid=batch_uuid,
            rate_limiter=rate_limiter,
            cost_tracker=cost_tracker,
            max_cost=max_cost,
            dry_run=dry_run,
        )
        total_written += res["written"]
        total_skipped += res["skipped"]
        total_failed += res["failed"]
        if res.get("halted_on_cost"):
            halted = True
            break

    logger.info(
        f"backfill_llm: LLM phase complete — written={total_written} "
        f"skipped={total_skipped} failed={total_failed} "
        f"cost=${cost_tracker['cumulative_usd']:.2f} "
        f"(in={cost_tracker['cumulative_tokens_in']} "
        f"out={cost_tracker['cumulative_tokens_out']})"
        + (" [HALTED ON COST]" if halted else "")
    )

    # Outcome attribution — plan §4 Phase C. One-shot for the batch.
    if not dry_run:
        try:
            from src.fno.llm_outcomes import attribute_llm_outcomes
            result = await attribute_llm_outcomes(dryrun_run_id=batch_uuid)
            logger.info(
                f"backfill_llm: attribution — examined={result.n_examined} "
                f"traded={result.n_traded} cf={result.n_counterfactual} "
                f"unobs={result.n_unobservable} timeout={result.n_timeout} "
                f"pending={result.n_still_pending}"
            )
        except Exception as exc:
            logger.warning(f"backfill_llm: attribution step failed: {exc!r}")

    await dispose_engine()
    return 1 if (total_failed or halted) else 0


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--from", dest="from_date", type=_parse_date, default=None)
    parser.add_argument("--to", dest="to_date", type=_parse_date, default=None)
    parser.add_argument(
        "--batch-id", type=str, required=True,
        help="Batch label (e.g. MoneyRatnam_backfill_v1). Hashed to a UUID via uuid5/NAMESPACE_DNS.",
    )
    parser.add_argument(
        "--holdout-tail-days", type=int, default=DEFAULT_HOLDOUT_TAIL_DAYS,
        help=f"Trading days reserved at the tail as out-of-sample holdout (default {DEFAULT_HOLDOUT_TAIL_DAYS}).",
    )
    parser.add_argument(
        "--rate-limit-per-min", type=int, default=_DEFAULT_RATE_LIMIT_PER_MIN,
        help="Token-bucket size in req/min (default 45 = 80%% of Anthropic tier-1 50).",
    )
    parser.add_argument(
        "--max-cost", type=float, default=None,
        help="USD cap on cumulative spend. Script halts mid-day if exceeded.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Walk the loop but do not call Claude — useful for validating the iteration order.",
    )
    args = parser.parse_args()

    raise SystemExit(asyncio.run(main(
        days=args.days,
        from_date=args.from_date,
        to_date=args.to_date,
        batch_label=args.batch_id,
        holdout_tail_days=args.holdout_tail_days,
        rate_limit_per_min=args.rate_limit_per_min,
        max_cost=args.max_cost,
        dry_run=args.dry_run,
    )))
