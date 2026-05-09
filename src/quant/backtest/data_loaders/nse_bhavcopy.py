"""Backfill ``options_chain`` from NSE F&O bhavcopy archives.

The fetch + parse + cache pipeline already exists in
``src.dryrun.bhavcopy.fetch_fo_bhavcopy`` (handles UDiFF and legacy formats,
404 → BhavcopyMissingError, disk cache). This module is the row-to-DB
insert path:

  1. Call ``fetch_fo_bhavcopy(d)`` → normalised pandas DataFrame.
  2. Look up ``Instrument.id`` for each row's symbol; skip unknowns.
  3. Compute IV from ``settle_price`` using ``compute_iv`` (bisection).
  4. Upsert into ``options_chain`` with
     ``snapshot_at = d 15:30 IST`` and ``source = 'nse_bhavcopy'``.

Idempotency: ``options_chain`` PK is
``(instrument_id, snapshot_at, expiry_date, strike_price, option_type)``.
We use ``ON CONFLICT DO NOTHING`` so re-running the loader for the same date
inserts zero new rows.

Decision Note (IV):
  * The bhavcopy ships only OHLCV + OI + settle_price — no IV or Greeks.
  * Computing IV during ingest is more useful than leaving it NULL: the
    backtest feature store reads ATM IV per (underlying, day) and recomputing
    it on every read would be wasteful. The bisection ``compute_iv`` is
    ~0.5 ms/row; a 5k-row bhavcopy ingests in < 5 s including IV.
  * Risk-free rate for IV defaults to 6.5% (RBI long-term mid). Callers can
    override via ``risk_free_rate`` for sensitivity analyses.
"""
from __future__ import annotations

import time as _time
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable

import pandas as pd
import pytz
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db import session_scope
from src.dryrun.bhavcopy import BhavcopyMissingError, fetch_fo_bhavcopy
from src.fno.chain_parser import compute_iv
from src.models.fno_chain import OptionsChain
from src.models.instrument import Instrument
from src.quant.backtest.clock import trading_days_between


_IST = pytz.timezone("Asia/Kolkata")
_DEFAULT_RISK_FREE_RATE = 0.065
_BHAVCOPY_SOURCE = "nse_bhavcopy"


async def _build_symbol_to_id_map(session) -> dict[str, str]:
    """Return ``{symbol_upper: instrument_id}`` for all active instruments."""
    rows = (await session.execute(
        select(Instrument.id, Instrument.symbol).where(
            Instrument.is_active.is_(True)
        )
    )).all()
    return {sym.upper(): inst_id for inst_id, sym in rows}


def _compute_dte_years(snapshot_date: date, expiry_date: date) -> float:
    """Calendar-day fraction from snapshot to expiry close (15:30 IST)."""
    if expiry_date < snapshot_date:
        return 0.0
    days = (expiry_date - snapshot_date).days
    # Snapshot is at 15:30 of the trade date; expiry is 15:30 of expiry date.
    return max(0.0, days / 365.0)


def _is_missing(value: Any) -> bool:
    """True if ``value`` is None or pandas-NaN. Pandas float NaN does not == None."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


async def load_one_date(
    snapshot_date: date,
    *,
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
    source_tag: str = _BHAVCOPY_SOURCE,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Load one date's F&O bhavcopy into ``options_chain``.

    Args (CLAUDE.md convention):
        as_of: Accepted but unused — bulk-historical load uses
            ``snapshot_date`` directly.
        dryrun_run_id: Propagated to ``options_chain.dryrun_run_id`` so a
            dry-run pass can be cleaned up.

    Returns ``{"rows_in_csv": N, "inserted": M, "skipped_unknown_symbol": K,
    "skipped_invalid": J}``.
    """
    try:
        df = await fetch_fo_bhavcopy(snapshot_date)
    except BhavcopyMissingError:
        logger.info(
            f"nse_bhavcopy: no archive for {snapshot_date} (likely holiday/weekend)"
        )
        return {
            "rows_in_csv": 0,
            "inserted": 0,
            "skipped_unknown_symbol": 0,
            "skipped_invalid": 0,
        }

    if df is None or df.empty:
        return {
            "rows_in_csv": 0,
            "inserted": 0,
            "skipped_unknown_symbol": 0,
            "skipped_invalid": 0,
        }

    snapshot_at = _IST.localize(datetime.combine(snapshot_date, time(15, 30)))

    inserted = 0
    skipped_unknown_symbol = 0
    skipped_invalid = 0

    async with session_scope() as session:
        sym_to_id = await _build_symbol_to_id_map(session)

        for row in df.itertuples(index=False):
            sym = str(getattr(row, "symbol", "") or "").strip().upper()
            opt_type = str(getattr(row, "option_type", "") or "").strip().upper()
            if not sym or opt_type not in ("CE", "PE"):
                skipped_invalid += 1
                continue
            inst_id = sym_to_id.get(sym)
            if inst_id is None:
                skipped_unknown_symbol += 1
                continue

            expiry = getattr(row, "expiry_date", None)
            strike = getattr(row, "strike_price", None)
            settle = getattr(row, "settle_price", None)
            close_px = getattr(row, "close", None)
            underlying_ltp = getattr(row, "underlying_price", None)
            volume = getattr(row, "contracts", None)
            oi = getattr(row, "oi", None)
            oi_change = getattr(row, "change_in_oi", None)

            # Strike and expiry are required by the schema's PK.
            # Pandas NaN floats are not None — explicit ``_is_missing`` covers
            # both, so DataFrames built from CSVs with empty cells skip cleanly.
            if _is_missing(expiry) or _is_missing(strike):
                skipped_invalid += 1
                continue
            if not isinstance(expiry, date):
                skipped_invalid += 1
                continue

            # Use settle price as LTP — that's what the daily close is.
            ltp_value = settle if not _is_missing(settle) else close_px
            if _is_missing(ltp_value):
                ltp_value = None
            if _is_missing(underlying_ltp):
                underlying_ltp = None
            iv: float | None = None
            if (
                underlying_ltp is not None
                and ltp_value is not None
                and float(ltp_value) > 0
                and float(underlying_ltp) > 0
            ):
                T = _compute_dte_years(snapshot_date, expiry)
                iv = compute_iv(
                    market_price=float(ltp_value),
                    S=float(underlying_ltp),
                    K=float(strike),
                    T=T,
                    r=risk_free_rate,
                    opt=opt_type,
                )

            stmt = (
                pg_insert(OptionsChain)
                .values(
                    instrument_id=inst_id,
                    snapshot_at=snapshot_at,
                    expiry_date=expiry,
                    strike_price=Decimal(str(strike)),
                    option_type=opt_type,
                    ltp=Decimal(str(ltp_value)) if ltp_value is not None else None,
                    volume=int(volume) if not _is_missing(volume) else None,
                    oi=int(oi) if not _is_missing(oi) else None,
                    oi_change=int(oi_change) if not _is_missing(oi_change) else None,
                    iv=iv if iv is not None else None,
                    underlying_ltp=(
                        Decimal(str(underlying_ltp))
                        if underlying_ltp is not None
                        else None
                    ),
                    source=source_tag,
                    dryrun_run_id=dryrun_run_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "instrument_id",
                        "snapshot_at",
                        "expiry_date",
                        "strike_price",
                        "option_type",
                    ]
                )
            )
            await session.execute(stmt)
            inserted += 1

    # Note: per-date duration is captured by the caller in ``backfill``;
    # ``load_one_date`` itself logs counts only.
    logger.info(
        f"nse_bhavcopy: {snapshot_date} loaded "
        f"rows={len(df)} inserted={inserted} "
        f"skipped_unknown={skipped_unknown_symbol} skipped_invalid={skipped_invalid}"
    )
    return {
        "rows_in_csv": len(df),
        "inserted": inserted,
        "skipped_unknown_symbol": skipped_unknown_symbol,
        "skipped_invalid": skipped_invalid,
    }


async def backfill(
    start_date: date,
    end_date: date,
    *,
    holidays: Iterable[date] = (),
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Backfill ``options_chain`` for every trading day in ``[start, end]``.

    Sequential per-date (NSE rate-limits aggressively). Per-date errors are
    logged and the loop continues — a single bad day doesn't kill the
    backfill.

    ``as_of`` and ``dryrun_run_id`` follow the CLAUDE.md convention.
    ``dryrun_run_id`` propagates to per-date inserts; ``as_of`` is unused.
    """
    days = trading_days_between(start_date, end_date, holidays=holidays)
    logger.info(
        f"nse_bhavcopy: backfilling {len(days)} trading days "
        f"in {start_date}..{end_date}"
    )
    t0 = _time.monotonic()
    totals = {
        "days": len(days),
        "rows_in_csv": 0,
        "inserted": 0,
        "skipped_unknown_symbol": 0,
        "skipped_invalid": 0,
        "failed_days": 0,
    }
    for d in days:
        try:
            res = await load_one_date(
                d,
                risk_free_rate=risk_free_rate,
                dryrun_run_id=dryrun_run_id,
            )
            for k, v in res.items():
                totals[k] = totals.get(k, 0) + v
        except Exception as exc:
            logger.warning(f"nse_bhavcopy: {d} failed: {exc!r}")
            totals["failed_days"] += 1
    totals["duration_sec"] = round(_time.monotonic() - t0, 2)
    logger.info(f"nse_bhavcopy: done. {totals}")
    return totals
