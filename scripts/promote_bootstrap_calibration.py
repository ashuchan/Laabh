"""Fit + (optionally) promote bootstrap calibration models from backfilled data.

Plan reference: docs/llm_feature_generator/backfill_plan.md §4 Phase D.

For a given batch UUID:
  1. Look up the holdout window in ``backfill_holdout_sentinels``.
  2. For each (feature, instrument_tier) pair, fit a Platt / isotonic model
     on the FIT set (rows with ``run_date < holdout_start``) and score it
     on the HOLDOUT window (rows in ``[holdout_start, holdout_end]``).
  3. Print a tabular summary: N, method, cv_ece, holdout_ece,
     cv_residual_var, holdout_residual_var.
  4. If ``--promote``, activate models that beat the holdout thresholds
     (default: ``holdout_ece < 0.10`` and ``holdout_residual_var < 1.5``)
     for at least 2 (feature, tier) pairs.

Promote is OFF by default. The plan §4 Phase E explicitly recommends NOT
promoting bootstrap to live scope on Sunday — the 10-day shadow safety
period in the main plan §3 takes precedence. Use ``--promote`` only when
you (the operator) have reviewed the metrics and want to ship.

Usage::

    python scripts/promote_bootstrap_calibration.py --batch-id MoneyRatnam_backfill_v1
    python scripts/promote_bootstrap_calibration.py --batch-id MoneyRatnam_backfill_v1 --promote
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
from typing import Iterable

from loguru import logger
from sqlalchemy import text

from src.db import dispose_engine, session_scope
from src.fno.backfill_batch import batch_label_to_uuid
from src.fno.calibration import (
    CalibrationModel,
    _persist_model,
    _set_active,
    fit_calibration,
    promote_if_better,
)

# Promotion thresholds. Plan §4 Phase D step 7.
_HOLDOUT_ECE_MAX = 0.10
_HOLDOUT_RV_MAX = 1.50
_MIN_PROMOTABLE_PAIRS = 2


async def _fetch_holdout_window(batch_uuid) -> tuple[date, date] | None:
    """Read the holdout window the backfill script recorded for this batch."""
    async with session_scope() as session:
        row = (await session.execute(text("""
            SELECT holdout_start_date, holdout_end_date
            FROM backfill_holdout_sentinels
            WHERE batch_uuid = :u
        """), {"u": str(batch_uuid)})).first()
    if row is None:
        return None
    return row.holdout_start_date, row.holdout_end_date


def _render_table(fits: Iterable[CalibrationModel]) -> None:
    """Compact text table — easier to scan than the per-row log lines."""
    rows = list(fits)
    if not rows:
        print("(no fits)")
        return
    headers = ("feature", "tier", "method", "N", "cv_ece", "holdout_ece",
               "cv_rv", "holdout_rv")
    line_fmt = "{:<22} {:<8} {:<10} {:<6} {:>9} {:>12} {:>9} {:>12}"
    print(line_fmt.format(*headers))
    print("-" * 96)
    for m in rows:
        def fmt(v):
            return f"{v:.4f}" if v is not None else "—"
        print(line_fmt.format(
            m.feature_name[:22],
            m.instrument_tier[:8],
            m.method[:10],
            m.n_observations,
            fmt(m.cv_ece),
            fmt(m.holdout_ece),
            fmt(m.cv_residual_var),
            fmt(m.holdout_residual_var),
        ))


def _passes_promotion(m: CalibrationModel) -> bool:
    if m.holdout_ece is None or m.holdout_residual_var is None:
        return False
    return m.holdout_ece < _HOLDOUT_ECE_MAX and m.holdout_residual_var < _HOLDOUT_RV_MAX


async def main(
    *,
    batch_label: str,
    promote: bool,
    lookback_days: int,
    prompt_version: str,
    phase: str,
    features: tuple[str, ...],
    tiers: tuple[str, ...],
) -> int:
    batch_uuid = batch_label_to_uuid(batch_label)
    holdout_range = await _fetch_holdout_window(batch_uuid)
    if holdout_range is None:
        logger.error(
            f"promote_bootstrap: no holdout sentinel for batch_uuid={batch_uuid}. "
            "Run backfill_llm_features.py first."
        )
        await dispose_engine()
        return 1
    holdout_start, holdout_end = holdout_range
    logger.info(
        f"promote_bootstrap: batch_uuid={batch_uuid} "
        f"holdout=[{holdout_start} → {holdout_end}]"
    )

    fits: list[CalibrationModel] = []
    for feature_name in features:
        for tier in tiers:
            try:
                model = await fit_calibration(
                    feature_name=feature_name,
                    prompt_version=prompt_version,
                    phase=phase,
                    instrument_tier=tier,
                    dryrun_run_id=batch_uuid,
                    lookback_days=lookback_days,
                    exclude_dates_after=holdout_start,
                    holdout_start=holdout_start,
                    holdout_end=holdout_end,
                )
            except Exception as exc:
                logger.warning(
                    f"promote_bootstrap: fit failed for {feature_name}/{tier}: {exc!r}"
                )
                continue
            if model is None:
                logger.info(
                    f"promote_bootstrap: {feature_name}/{tier} skipped — "
                    "insufficient sample"
                )
                continue
            # Persist every fit (without activating) so the dashboard can read them.
            persisted_id = await _persist_model(model)
            logger.info(
                f"promote_bootstrap: persisted fit id={persisted_id} "
                f"{feature_name}/{tier} N={model.n_observations} "
                f"cv_ece={model.cv_ece} holdout_ece={model.holdout_ece}"
            )
            fits.append(model)

    print("\nBootstrap calibration fits:")
    _render_table(fits)

    if not promote:
        logger.info(
            "promote_bootstrap: --promote not set — fits persisted with "
            "is_active=false, no live activation."
        )
        await dispose_engine()
        return 0

    promotable = [m for m in fits if _passes_promotion(m)]
    if len(promotable) < _MIN_PROMOTABLE_PAIRS:
        logger.warning(
            f"promote_bootstrap: only {len(promotable)} of {len(fits)} fits "
            f"meet the holdout thresholds "
            f"(ece<{_HOLDOUT_ECE_MAX}, rv<{_HOLDOUT_RV_MAX}). "
            f"Need ≥{_MIN_PROMOTABLE_PAIRS} for promotion. Skipping promote."
        )
        for m in fits:
            if not _passes_promotion(m):
                logger.info(
                    f"  reject {m.feature_name}/{m.instrument_tier}: "
                    f"holdout_ece={m.holdout_ece} holdout_rv={m.holdout_residual_var}"
                )
        await dispose_engine()
        return 0

    # Promote — re-use the standard promotion path, which (when holdout
    # metrics are populated) prefers holdout_ece vs the live-scope
    # active row.
    n_promoted = 0
    for m in promotable:
        # Strip the dryrun_run_id when activating into live scope — the
        # live calibration consumer scopes to dryrun_run_id IS NULL.
        live_model = CalibrationModel(
            method=m.method,
            params=m.params,
            n_observations=m.n_observations,
            cv_ece=m.cv_ece,
            cv_residual_var=m.cv_residual_var,
            feature_name=m.feature_name,
            prompt_version=m.prompt_version,
            phase=m.phase,
            instrument_tier=m.instrument_tier,
            holdout_ece=m.holdout_ece,
            holdout_residual_var=m.holdout_residual_var,
        )
        if await promote_if_better(live_model):
            n_promoted += 1
            logger.info(
                f"promote_bootstrap: PROMOTED {m.feature_name}/{m.instrument_tier} "
                f"(holdout_ece={m.holdout_ece:.4f})"
            )
        else:
            logger.info(
                f"promote_bootstrap: kept current — "
                f"{m.feature_name}/{m.instrument_tier} did not beat existing model"
            )

    logger.info(
        f"promote_bootstrap: complete — {n_promoted}/{len(promotable)} promoted, "
        f"{len(fits) - len(promotable)} below threshold, "
        f"{len(fits)} total fits."
    )
    await dispose_engine()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-id", type=str, required=True,
        help="Same batch label passed to backfill_llm_features.py.",
    )
    parser.add_argument(
        "--promote", action="store_true",
        help="Activate fits that pass holdout thresholds into the live scope. "
             "DEFAULT OFF — bootstrap is research, not production.",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=180,
        help="Rolling window of rows to consider (default 180 = matches the backfill).",
    )
    parser.add_argument("--prompt-version", default="v10_continuous")
    parser.add_argument("--phase", default="fno_thesis")
    parser.add_argument(
        "--features", nargs="+",
        default=["directional_conviction", "raw_confidence"],
    )
    parser.add_argument(
        "--tiers", nargs="+", default=["pooled", "T1", "T2"],
    )
    args = parser.parse_args()

    raise SystemExit(asyncio.run(main(
        batch_label=args.batch_id,
        promote=args.promote,
        lookback_days=args.lookback_days,
        prompt_version=args.prompt_version,
        phase=args.phase,
        features=tuple(args.features),
        tiers=tuple(args.tiers),
    )))
