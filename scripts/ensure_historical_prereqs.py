"""Backfill the historical inputs Phase 3 / outcome attribution need.

Plan reference: docs/llm_feature_generator/backfill_plan.md §4 Phase A.

For each trading day D in the window, this script ensures every input the
v10 prompt + bandit + per-tier calibration depends on is present in the DB:

  1. F&O + CM bhavcopy cached on disk (via src.dryrun.bhavcopy).
  2. price_daily backfilled via yfinance (one big call covering the window).
  3. iv_history.atm_iv + rank/percentile via iv_history_builder.build_for_date.
  4. iv_history.rv_20d + vrp + vrp_regime via vrp_engine.compute_vrp_for_date.
  5. vol_surface_snapshot via vol_surface.compute_for_instruments.
  6. market_regime_snapshot via regime_classifier.compute_regime
     (with as_of pinned to ist(D, 9, 0) so historical replay sees only
     pre-09:00 data).
  7. fno_candidates phase=1 + phase=2 via run_phase1 / run_phase2
     (with as_of and instrument_tier hydration).
  8. quant_universe_baseline (Phase 0.5 deterministic six-factor top-K=25)
     via run_deterministic_baseline.

Each step is idempotent — its underlying upsert / ON CONFLICT DO NOTHING
makes a re-run a no-op for already-populated days.

Usage::

    python scripts/ensure_historical_prereqs.py --days 180
    python scripts/ensure_historical_prereqs.py --from 2025-12-01 --to 2026-04-30
    python scripts/ensure_historical_prereqs.py --days 30 --skip-existing
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, time, timedelta
from typing import Iterable

import pytz
from loguru import logger
from sqlalchemy import select, text

from src.db import dispose_engine, session_scope
from src.quant.backtest.clock import trading_days_between

_IST = pytz.timezone("Asia/Kolkata")


def _ist_morning(d: date) -> datetime:
    """Return ``date`` localised to 09:00 IST as the canonical historical
    cutoff for the prereq computations (matches the backfill plan §3.2)."""
    return _IST.localize(datetime.combine(d, time(9, 0))).astimezone(pytz.UTC)


async def _has_phase2_rows(d: date) -> bool:
    """Sentinel for ``--skip-existing``: a day with phase=2 candidates is
    treated as fully prepped (Phase 1 must have run; bhavcopy must have
    been cached; iv_history must exist; etc.). Cheap one-row SELECT."""
    async with session_scope() as session:
        row = (await session.execute(
            text(
                "SELECT 1 FROM fno_candidates "
                "WHERE run_date = :d AND phase = 2 LIMIT 1"
            ),
            {"d": d},
        )).first()
    return row is not None


async def _ensure_one_date(d: date, *, as_of: datetime) -> dict[str, str]:
    """Run every prereq step for a single date. Returns a dict mapping
    step name → 'ok' | 'err: <msg>' for the per-day summary."""
    results: dict[str, str] = {}

    # --- 1. Bhavcopy (F&O + CM). Cached on disk; cheap re-fetch.
    try:
        from src.dryrun.bhavcopy import fetch_cm_bhavcopy, fetch_fo_bhavcopy
        await fetch_fo_bhavcopy(d)
        await fetch_cm_bhavcopy(d)
        results["bhavcopy"] = "ok"
    except Exception as exc:
        results["bhavcopy"] = f"err: {exc!r}"

    # --- 3. IV history (atm_iv + rank/pct).
    try:
        from src.fno.iv_history_builder import build_for_date as build_iv_history
        n = await build_iv_history(d)
        results["iv_history"] = f"ok ({n} rows)"
    except Exception as exc:
        results["iv_history"] = f"err: {exc!r}"

    # --- 4. VRP (rv_20d + vrp + vrp_regime).
    try:
        from src.fno.vrp_engine import compute_vrp_for_date
        n = await compute_vrp_for_date(d)
        results["vrp"] = f"ok ({n} rows)"
    except Exception as exc:
        results["vrp"] = f"err: {exc!r}"

    # --- 5. Vol surface.
    try:
        from src.fno.vol_surface import compute_for_instruments
        n = await compute_for_instruments(run_date=d)
        results["vol_surface"] = f"ok ({n} rows)"
    except Exception as exc:
        results["vol_surface"] = f"err: {exc!r}"

    # --- 6. Regime snapshot (as_of pinned to 09:00 IST).
    try:
        from src.fno.regime_classifier import compute_regime
        r = await compute_regime(d, as_of=as_of)
        results["regime"] = f"ok ({r.regime})"
    except Exception as exc:
        results["regime"] = f"err: {exc!r}"

    # --- 7a. Phase 1 (with as_of so chain queries are point-in-time).
    try:
        from src.fno.universe import run_phase1
        p1 = await run_phase1(d, as_of=as_of)
        results["phase1"] = f"ok ({sum(1 for r in p1 if r.passed)} passed / {len(p1)})"
    except Exception as exc:
        results["phase1"] = f"err: {exc!r}"

    # --- 7b. Phase 2 (with as_of).
    try:
        from src.fno.catalyst_scorer import run_phase2
        p2 = await run_phase2(d, as_of=as_of)
        results["phase2"] = f"ok ({sum(1 for r in p2 if r.passed)} passed / {len(p2)})"
    except Exception as exc:
        results["phase2"] = f"err: {exc!r}"

    # --- 8. Deterministic Phase 0.5 baseline.
    try:
        from src.fno.deterministic_universe import run_deterministic_baseline
        n = await run_deterministic_baseline(d, as_of=as_of)
        results["det_baseline"] = f"ok ({n} rows)"
    except Exception as exc:
        results["det_baseline"] = f"err: {exc!r}"

    return results


async def _backfill_price_daily(days: int) -> None:
    """One-shot yfinance pull covering the entire window. Cheaper than
    per-date pulls because yfinance fetches a range per ticker."""
    try:
        from src.collectors.yahoo_finance import YahooFinanceCollector
        # Pad slightly — we want enough lookback for the 60-day rolling
        # windows the deterministic baseline uses.
        collector = YahooFinanceCollector(days=max(days + 90, 365), symbols=None)
        result = await collector.run()
        logger.info(
            f"prereqs: yfinance backfill fetched={result.items_fetched} "
            f"new={result.items_new} errors={len(result.errors)}"
        )
    except Exception as exc:
        logger.warning(f"prereqs: yfinance backfill failed: {exc!r}")


async def main(
    *,
    days: int,
    from_date: date | None,
    to_date: date | None,
    skip_existing: bool,
    holidays: Iterable[date] | None = None,
) -> int:
    end = to_date or (date.today() - timedelta(days=1))
    start = from_date or (end - timedelta(days=int(days * 1.5)))   # 1.5× buffer for weekends

    # Holidays default: read from database/nse_holidays.json. The loader
    # logs a single WARNING if the file is missing so the operator sees
    # the data gap (weekends will still be filtered).
    if holidays is None:
        from src.fno.nse_holidays import load_nse_holidays
        holidays = load_nse_holidays(start, end)

    trading_days = trading_days_between(start, end, holidays=holidays)
    # Truncate to the requested count (most recent N).
    if from_date is None:
        trading_days = trading_days[-days:]

    if not trading_days:
        logger.warning(f"prereqs: no trading days between {start} and {end}")
        return 0

    logger.info(
        f"prereqs: processing {len(trading_days)} trading days "
        f"({trading_days[0]} → {trading_days[-1]})"
    )

    # 2. yfinance once for the whole window.
    await _backfill_price_daily(len(trading_days))

    total = len(trading_days)
    succeeded = failed = skipped = 0
    for idx, d in enumerate(trading_days, start=1):
        if skip_existing and await _has_phase2_rows(d):
            skipped += 1
            logger.info(f"prereqs: [{idx}/{total}] {d} — already populated, skipping")
            continue
        as_of = _ist_morning(d)
        logger.info(f"prereqs: [{idx}/{total}] {d} (as_of {as_of.isoformat()})")
        results = await _ensure_one_date(d, as_of=as_of)
        bad_steps = [k for k, v in results.items() if v.startswith("err:")]
        if bad_steps:
            failed += 1
            logger.warning(f"prereqs: {d} had failures in: {bad_steps}")
            for k, v in results.items():
                logger.info(f"  {k}: {v}")
        else:
            succeeded += 1
            logger.info(
                f"prereqs: {d} ✓ "
                + " | ".join(f"{k}={v}" for k, v in results.items())
            )

    logger.info(
        f"prereqs: complete — ok={succeeded} failed={failed} "
        f"skipped={skipped} (total {total})"
    )
    await dispose_engine()
    return 0 if failed == 0 else 1


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=180,
        help="Most recent N trading days to backfill (default 180).",
    )
    parser.add_argument(
        "--from", dest="from_date", type=_parse_date, default=None,
        help="Inclusive start date (YYYY-MM-DD). Overrides --days when set.",
    )
    parser.add_argument(
        "--to", dest="to_date", type=_parse_date, default=None,
        help="Inclusive end date (YYYY-MM-DD). Default = yesterday.",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="Skip dates that already have phase=2 candidates.",
    )
    args = parser.parse_args()

    raise SystemExit(asyncio.run(main(
        days=args.days,
        from_date=args.from_date,
        to_date=args.to_date,
        skip_existing=args.skip_existing,
    )))
