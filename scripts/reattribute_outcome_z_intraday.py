"""Stage 2 re-attribution: re-compute ``outcome_z`` from intraday data.

Plan reference: docs/llm_feature_generator/backfill_plan.md §5 Phase G.

After ``scripts/backfill_llm_features.py`` (Stage 1) has populated
``llm_decision_log`` for a batch UUID using EOD bhavcopy-close
counterfactual outcomes, this script re-runs outcome attribution for the
same rows using the higher-fidelity intraday bars in ``price_intraday``
(populated by the Dhan backfill — plan §5 Phase F).

Behaviour per row:
  * If ``price_intraday`` has no coverage for (instrument, run_date),
    the existing outcome (``outcome_class='counterfactual'``) is left
    untouched.
  * Otherwise the underlying P&L is recomputed using
      entry_price = first intraday close at-or-after 09:30 IST on D
      exit_price  = last intraday close at-or-before 15:15 IST on D
        (or the row's ``proposed_horizon`` cap, whichever is earlier).
    Outcome P&L is the direction-signed simple return on the underlying;
    ``outcome_z = outcome_pnl_pct / expected_vol_at_entry``.
  * The row's ``outcome_class`` is updated to
    ``'counterfactual_intraday'`` (new Stage 2 label) and
    ``outcome_attributed_at`` bumped.

This script does NOT re-run the LLM — it only updates the outcome
columns. The v10 feature values stay as Stage 1 wrote them.

Intentional simplifications (documented for the reader):
  * Direction = ``sign(directional_conviction)``. Long-call / long-put
    structures dominate the v10 universe, so this captures the bulk of
    intended exposure without re-pricing every chain leg via Black-Scholes
    on the intraday underlying — that's a Stage 3 refinement.
  * No structure-specific leg pricing here. The plan's Phase G describes a
    full Black-Scholes synthesizer using intraday underlying + EOD IV
    smile. The harness for that exists in
    ``src/quant/backtest/data_loaders/`` and is wired into the backtest
    runner, not the bootstrap calibration. Using the underlying-return
    proxy is a deliberate Stage-2 narrowing: it's the variance metric the
    calibration models score against, not a trade-level P&L.

Usage::

    python scripts/reattribute_outcome_z_intraday.py --batch-id MoneyRatnam_backfill_v1
    python scripts/reattribute_outcome_z_intraday.py --batch-id MoneyRatnam_backfill_v1 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytz
from loguru import logger
from sqlalchemy import select, text, update

from src.config import get_settings
from src.db import dispose_engine, session_scope
from src.fno.backfill_batch import batch_label_to_uuid
from src.models.llm_decision_log import LLMDecisionLog

_IST = pytz.timezone("Asia/Kolkata")
_TRADING_DAYS_PER_YEAR = 252


def _parse_hhmm(s: str, *, default: time) -> time:
    """Parse a ``HH:MM`` or ``HH:MM:SS`` string. Fall back to ``default``
    with a warning when the value is malformed.

    ``Settings.market_open_time`` is typed ``str`` with no shape validator,
    so an operator setting ``MARKET_OPEN_TIME=09:15:00`` (seconds-suffix,
    perfectly valid time format) would crash naive ``HH:MM`` parsing.
    """
    if not s:
        return default
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).time()
        except ValueError:
            continue
    logger.warning(
        f"_parse_hhmm: could not parse {s!r} — falling back to {default}"
    )
    return default


def _entry_exit_times() -> tuple[time, time]:
    """Derive the intraday-attribution entry + exit times from config.

    Entry: market open (09:15 IST) + Phase 4's no-entry-before window
    (default 30 min) → 09:45 IST. This matches the live entry-executor
    convention of waiting past the morning opening volatility.

    Exit: the quant hard-exit time (default 14:30 IST) — no new entries
    after this. The exit valuation uses the last intraday close at-or-
    before this cutoff, matching how live positions are flattened.

    Reading from settings keeps Stage 2 outcome attribution aligned with
    whatever the live config says, so the operator changing Phase 4
    timing also moves Stage 2 outcome boundaries.
    """
    cfg = get_settings()
    open_t = _parse_hhmm(cfg.market_open_time, default=time(9, 15))
    entry_minute_offset = cfg.fno_phase4_no_entry_before_minutes
    total_min = open_t.hour * 60 + open_t.minute + entry_minute_offset
    # Clamp to a valid 24-hour time so a pathological offset doesn't wrap.
    total_min = max(0, min(total_min, 23 * 60 + 59))
    entry = time(total_min // 60, total_min % 60)
    exit_t = cfg.laabh_quant_hard_exit_time  # already a `time` object
    return entry, exit_t


def _ist_dt(d: date, t: time) -> datetime:
    return _IST.localize(datetime.combine(d, t)).astimezone(timezone.utc)


async def _fetch_intraday_endpoints(
    session, *, instrument_id, run_date: date
) -> tuple[float, float] | None:
    """Return ``(entry_close, exit_close)`` from price_intraday for the day,
    or ``None`` when the day has no intraday coverage for the instrument."""
    entry_t, exit_t = _entry_exit_times()
    lo = _ist_dt(run_date, entry_t)
    hi = _ist_dt(run_date, exit_t)
    entry_row = (await session.execute(text("""
        SELECT close FROM price_intraday
        WHERE instrument_id = :i AND timestamp >= :lo AND timestamp <= :hi
        ORDER BY timestamp ASC
        LIMIT 1
    """), {"i": str(instrument_id), "lo": lo, "hi": hi})).first()
    if entry_row is None:
        return None
    exit_row = (await session.execute(text("""
        SELECT close FROM price_intraday
        WHERE instrument_id = :i AND timestamp >= :lo AND timestamp <= :hi
        ORDER BY timestamp DESC
        LIMIT 1
    """), {"i": str(instrument_id), "lo": lo, "hi": hi})).first()
    if exit_row is None:
        return None
    entry = float(entry_row.close)
    exit_ = float(exit_row.close)
    if entry <= 0 or exit_ <= 0:
        return None
    return entry, exit_


async def _fetch_expected_vol(session, *, instrument_id, run_date: date) -> float | None:
    """Single-day expected vol from ``iv_history.rv_20d`` at run_date.

    Holding window for the intraday outcome is one trading day, so
    σ_position = rv_annual × √(1/252).
    """
    row = (await session.execute(text("""
        SELECT rv_20d FROM iv_history
        WHERE instrument_id = :i
          AND date <= :d
          AND rv_20d IS NOT NULL
          AND dryrun_run_id IS NULL
        ORDER BY date DESC LIMIT 1
    """), {"i": str(instrument_id), "d": run_date})).first()
    if row is None or row.rv_20d is None:
        return None
    rv_annual = float(row.rv_20d)
    return rv_annual * (1.0 / _TRADING_DAYS_PER_YEAR) ** 0.5


def _direction_sign(directional_conviction) -> int:
    """+1 for bullish v10 conviction, -1 for bearish.

    Exactly-zero conviction returns 0 and the row is skipped — a v10 row
    with directional_conviction=0 is rare and signals the model couldn't
    pick a side, so attributing a directional outcome would be misleading.
    A previous deadband of ±0.05 was removed: the calibration model is
    the right place to learn how to interpret small-magnitude conviction,
    not a hardcoded threshold in the attribution layer.
    """
    if directional_conviction is None:
        return 0
    val = float(directional_conviction)
    if val > 0:
        return 1
    if val < 0:
        return -1
    return 0


# outcome_class values we are allowed to overwrite. 'traded' rows should
# never appear under a backfill UUID (no live signal exists for a historical
# replay), but we guard anyway — overwriting a real trade outcome with a
# synthetic counterfactual_intraday would silently corrupt calibration data.
# 'unobservable' and 'timeout' are EOD-attribution disposition labels that
# we DO want to re-try at intraday resolution. NULL means the attribution
# job hasn't run yet — also a valid re-attribute target.
_REATTRIBUTABLE_CLASSES = frozenset({
    "counterfactual",            # Stage 1 attribution default
    "counterfactual_eod",        # Stage 2 taxonomy variant (currently unused — kept for forward-compat)
    "unobservable",              # EOD attribution gave up; intraday data may rescue
    "timeout",                   # T+30 stalled; same rationale
})


async def _load_batch_rows(batch_uuid) -> list[LLMDecisionLog]:
    """Pull llm_decision_log rows scoped to this batch that are safe to
    re-attribute.

    Filters by ``outcome_class`` (see ``_REATTRIBUTABLE_CLASSES``) so we
    never overwrite a 'traded' row — even though one should not exist
    under a backfill batch UUID, the guard prevents silent data
    corruption if someone ever shares a batch_uuid across live + backfill.
    NULL outcome_class is also accepted (attribution job hasn't run yet).
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(LLMDecisionLog).where(
                LLMDecisionLog.dryrun_run_id == batch_uuid,
                # Postgres: NULL passes the OR branch; explicit list otherwise.
                (LLMDecisionLog.outcome_class.is_(None))
                | (LLMDecisionLog.outcome_class.in_(_REATTRIBUTABLE_CLASSES)),
            ).order_by(LLMDecisionLog.run_date.asc(), LLMDecisionLog.id.asc())
        )).scalars().all()
    return list(rows)


async def _reattribute_one(row: LLMDecisionLog) -> str:
    """Process one row. Returns the disposition label for the per-row summary."""
    async with session_scope() as session:
        endpoints = await _fetch_intraday_endpoints(
            session, instrument_id=row.instrument_id, run_date=row.run_date
        )
        if endpoints is None:
            return "no_intraday"
        entry, exit_ = endpoints
        sign = _direction_sign(row.directional_conviction)
        if sign == 0:
            return "neutral_direction"

        pnl_pct = sign * (exit_ - entry) / entry
        expected_vol = await _fetch_expected_vol(
            session, instrument_id=row.instrument_id, run_date=row.run_date
        )
        outcome_z = (pnl_pct / expected_vol) if expected_vol and expected_vol > 0 else None

        await session.execute(
            update(LLMDecisionLog)
            .where(LLMDecisionLog.id == row.id)
            .values(
                outcome_class="counterfactual_intraday",
                outcome_pnl_pct=Decimal(f"{pnl_pct:.6f}"),
                outcome_z=Decimal(f"{outcome_z:.6f}") if outcome_z is not None else None,
                outcome_attributed_at=datetime.now(tz=timezone.utc),
            )
        )
    return "intraday_attributed"


async def main(*, batch_label: str, dry_run: bool) -> int:
    batch_uuid = batch_label_to_uuid(batch_label)
    rows = await _load_batch_rows(batch_uuid)
    logger.info(f"reattribute_intraday: {len(rows)} rows in batch {batch_uuid}")
    if not rows:
        logger.warning("reattribute_intraday: nothing to do")
        await dispose_engine()
        return 0

    counts: dict[str, int] = {
        "intraday_attributed": 0,
        "no_intraday": 0,
        "neutral_direction": 0,
        "error": 0,
    }
    pnl_values: list[float] = []

    for idx, row in enumerate(rows, start=1):
        if dry_run:
            # In dry-run mode just probe whether intraday data exists.
            async with session_scope() as session:
                endpoints = await _fetch_intraday_endpoints(
                    session, instrument_id=row.instrument_id, run_date=row.run_date
                )
            disposition = "intraday_available" if endpoints is not None else "no_intraday"
            counts.setdefault(disposition, 0)
            counts[disposition] += 1
        else:
            try:
                disposition = await _reattribute_one(row)
                counts[disposition] += 1
                if disposition == "intraday_attributed" and row.outcome_pnl_pct is not None:
                    pnl_values.append(float(row.outcome_pnl_pct))
            except Exception as exc:
                logger.warning(
                    f"reattribute_intraday: row id={row.id} "
                    f"({row.run_date}/{row.instrument_id}) failed: {exc!r}"
                )
                counts["error"] += 1

        if idx % 100 == 0:
            logger.info(f"reattribute_intraday: progress {idx}/{len(rows)} — {counts}")

    logger.info(f"reattribute_intraday: complete — {counts}")
    if pnl_values:
        import statistics
        logger.info(
            f"reattribute_intraday: outcome_pnl_pct distribution — "
            f"mean={statistics.mean(pnl_values):+.4f} "
            f"stdev={statistics.stdev(pnl_values) if len(pnl_values) > 1 else 0:.4f} "
            f"min={min(pnl_values):+.4f} max={max(pnl_values):+.4f}"
        )
    await dispose_engine()
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-id", type=str, required=True,
        help="Batch label that backfill_llm_features.py used.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report intraday coverage without writing any updates.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(
        batch_label=args.batch_id,
        dry_run=args.dry_run,
    )))
