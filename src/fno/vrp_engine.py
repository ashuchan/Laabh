"""Volatility Risk Premium (VRP) Engine.

VRP = ATM_IV (annualized, decimal) - RV_20d (Yang-Zhang realized vol, annualized decimal)

Interpretation:
  VRP > +0.02  → 'rich'  : IV overpriced by 2+ vol points → premium-selling edge
                            (iron condors, credit spreads, short strangles)
  VRP < -0.01  → 'cheap' : IV below realized → tail risk building or buying opportunity
  otherwise    → 'fair'  : no strong vol-premium signal; rely on directional catalysts

Research basis:
  Carr & Wu (2009), "Variance Risk Premiums", Review of Financial Studies.
  Bhattacharyya & Madhavan (NSE Working Paper, 2019) — VRP persistence in Indian markets.

Realized volatility estimator: Yang-Zhang (2000), minimum-variance unbiased OHLC
estimator. Handles overnight gaps correctly (critical on earnings/macro event days).
Falls back to close-to-close if OHLC data is incomplete for > 50% of the window.

Unit convention: all IV and RV values are stored as *annualized decimals*
  (0.20 = 20% annual volatility). The raw `atm_iv` column in iv_history may have been
  written in percentage-point form by older ingestion code; `_to_decimal_iv()` normalizes
  using the invariant that no Indian-listed equity has annualized vol > 300% (decimal 3.0).

Integration:
  - Called by orchestrator.run_eod_tasks() AFTER iv_history_builder.build_for_date()
  - thesis_synthesizer reads VRP via _get_iv_snapshot()
  - entry_engine reads vrp_regime to bias strategy-type selection
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from loguru import logger
from sqlalchemy import select

from src.config import get_settings
from src.db import session_scope
from src.models.fno_iv import IVHistory
from src.models.instrument import Instrument
from src.models.price import PriceDaily


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VRPResult:
    instrument_id: str
    symbol: str
    date: date
    atm_iv: float        # normalized decimal (e.g. 0.22 = 22% annual IV)
    rv_20d: float        # Yang-Zhang realized vol, annualized decimal
    vrp: float           # atm_iv - rv_20d
    vrp_regime: str      # 'rich' | 'fair' | 'cheap'


@dataclass(frozen=True)
class _PriceRow:
    """One day of OHLCV from price_daily, used for RV computation."""
    date: date
    open: float
    high: float
    low: float
    close: float


# ---------------------------------------------------------------------------
# Pure computation helpers (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def _to_decimal_iv(raw: float | None) -> float | None:
    """Normalize ATM IV to annualized decimal form (0.22 = 22% annual vol).

    Old iv_history rows may carry percentage-point values (e.g. 22.0) from an
    earlier ingestion bug. The invariant used for detection:
      No Indian-listed equity has annualized vol > 300% (decimal 3.0).
    So any raw value > 3.0 is safely treated as percentage-points and divided by 100.

    Guards:
      - None or NaN     → None (caller skips VRP computation)
      - zero or negative → None (corrupt or suspended-trading row)
    """
    if raw is None or math.isnan(raw) or raw <= 0.0:
        return None
    if raw > 3.0:
        return raw / 100.0
    return raw


def yang_zhang_rv(
    rows: Sequence[_PriceRow],
    *,
    annualize_factor: float = 252.0,
    max_single_day_log_return: float = 0.15,
) -> float | None:
    """Yang-Zhang (2000) realized volatility estimator with corporate-action filtering.

    Returns annualized RV as a decimal (e.g. 0.22 = 22% annual vol),
    or None if insufficient data (< 2 clean rows after filtering).

    Algorithm:
        σ²_YZ = σ²_overnight + k·σ²_cc + (1-k)·σ²_RS

        σ²_overnight = mean of squared overnight log-returns  ln(O_i / C_{i-1})
        σ²_cc        = sample variance of close-to-close log-returns  ln(C_i / C_{i-1})
        σ²_RS        = mean of Rogers-Satchell terms:
                         ln(H/O)·ln(H/C) + ln(L/O)·ln(L/C)
        k            = 0.34 / (1.34 + (n+1)/(n-1))   [optimal weight, YZ §4]

    Rows must be sorted oldest → newest. The first row is used only as the
    source of C_{i-1} for the second row's overnight return.

    Corporate action filtering:
        NSE has a 20% daily circuit limit. Any close-to-close log return beyond
        ±max_single_day_log_return (default ±15%) is flagged as a corporate action
        (demerger, rights issue, bonus, split) or data anomaly and excluded from
        all three YZ terms for that day. This prevents demerger price adjustments
        (e.g. VEDL 2026-04-30: ₹774→₹272 = -64.9% log return = -1.047) from
        making realized vol look like 300%+ and classifying a normal IV day as 'cheap'.

    Fallback to close-to-close if > 50% of rows are missing OHLC.
    """
    if len(rows) < 2:
        return None

    overnight_terms: list[float] = []
    cc_returns: list[float] = []
    rs_terms: list[float] = []
    ohlc_missing = 0
    corporate_action_skipped = 0

    for i in range(1, len(rows)):
        prev, curr = rows[i - 1], rows[i]
        if prev.close <= 0 or curr.close <= 0:
            continue

        r_cc = math.log(curr.close / prev.close)

        # Corporate action guard: skip this day if the close-to-close return
        # exceeds the threshold. The overnight and RS terms are also excluded
        # because they share the same structural break.
        if abs(r_cc) > max_single_day_log_return:
            corporate_action_skipped += 1
            continue

        cc_returns.append(r_cc)

        # Overnight return: open today vs close yesterday
        if curr.open and curr.open > 0:
            overnight_terms.append(math.log(curr.open / prev.close) ** 2)
        else:
            ohlc_missing += 1

        # Rogers-Satchell: uses intraday OHLC
        if curr.open and curr.high and curr.low and curr.open > 0:
            h, l, o, c = curr.high, curr.low, curr.open, curr.close
            try:
                rs = math.log(h / o) * math.log(h / c) + math.log(l / o) * math.log(l / c)
                rs_terms.append(rs)
            except (ValueError, ZeroDivisionError):
                ohlc_missing += 1
        else:
            ohlc_missing += 1

    if corporate_action_skipped > 0:
        logger.debug(
            f"yang_zhang_rv: excluded {corporate_action_skipped} day(s) with "
            f"|log_ret| > {max_single_day_log_return:.0%} (corporate action / data anomaly)"
        )

    n = len(cc_returns)
    if n < 2:
        return None

    # Fall back to pure close-to-close when OHLC is mostly missing
    ohlc_coverage = 1.0 - ohlc_missing / (2 * n)  # 2 checks per day (overnight + RS)
    if ohlc_coverage < 0.5:
        variance_daily = sum(r ** 2 for r in cc_returns) / n
        return math.sqrt(variance_daily * annualize_factor)

    # Yang-Zhang combination
    k = 0.34 / (1.34 + (n + 1) / (n - 1))

    sigma2_overnight = sum(overnight_terms) / len(overnight_terms) if overnight_terms else 0.0

    mean_cc = sum(cc_returns) / n
    sigma2_cc = sum((r - mean_cc) ** 2 for r in cc_returns) / (n - 1)

    sigma2_rs = sum(rs_terms) / len(rs_terms) if rs_terms else sigma2_cc

    sigma2_yz = sigma2_overnight + k * sigma2_cc + (1.0 - k) * sigma2_rs
    if sigma2_yz <= 0:
        return None

    return math.sqrt(sigma2_yz * annualize_factor)


def classify_vrp_regime(
    vrp: float,
    *,
    rich_threshold: float,
    cheap_threshold: float,
) -> str:
    """Map VRP value to regime label."""
    if vrp > rich_threshold:
        return "rich"
    if vrp < cheap_threshold:
        return "cheap"
    return "fair"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_atm_iv_for_date(
    session,
    instrument_id: str,
    target_date: date,
) -> float | None:
    """Read the atm_iv written by iv_history_builder for target_date."""
    row = (await session.execute(
        select(IVHistory.atm_iv)
        .where(
            IVHistory.instrument_id == instrument_id,
            IVHistory.date == target_date,
            IVHistory.dryrun_run_id.is_(None),
        )
    )).scalar_one_or_none()
    if row is None:
        return None
    return _to_decimal_iv(float(row))


async def _get_price_rows(
    session,
    instrument_id: str,
    target_date: date,
    lookback_days: int,
) -> list[_PriceRow]:
    """Fetch `lookback_days + 1` rows of OHLCV ending on target_date.

    We fetch one extra row so the Yang-Zhang estimator has C_{i-1} for the
    first computed day's overnight return. Rows with close <= 0 are excluded
    (suspended-trading or corporate-action artifacts).
    """
    cutoff = target_date - timedelta(days=lookback_days + 30)  # buffer for weekends/holidays
    result = await session.execute(
        select(
            PriceDaily.date,
            PriceDaily.open,
            PriceDaily.high,
            PriceDaily.low,
            PriceDaily.close,
        )
        .where(
            PriceDaily.instrument_id == instrument_id,
            PriceDaily.date >= cutoff,
            PriceDaily.date <= target_date,
            PriceDaily.close > 0,
        )
        .order_by(PriceDaily.date.asc())
        .limit(lookback_days + 5)  # +5 safety buffer
    )
    return [
        _PriceRow(
            date=r.date,
            open=float(r.open) if r.open else 0.0,
            high=float(r.high) if r.high else 0.0,
            low=float(r.low) if r.low else 0.0,
            close=float(r.close),
        )
        for r in result.all()
    ]


async def _upsert_vrp(
    session,
    instrument_id: str,
    target_date: date,
    rv_20d: float,
    vrp: float,
    vrp_regime: str,
) -> None:
    """Write VRP columns to the existing iv_history row for target_date.

    Uses ON CONFLICT DO UPDATE scoped to the VRP columns only — does not
    touch atm_iv, iv_rank_52w, iv_percentile_52w (those belong to builder).
    """
    # Use UPDATE-only (no INSERT fallback) to avoid the atm_iv NOT NULL
    # constraint problem. The iv_history row MUST already exist (written by
    # iv_history_builder) before VRP is computed — compute_vrp_for_date()
    # checks for the existing atm_iv row and skips when absent, so we should
    # never reach this code without a prior row. The UPDATE form makes this
    # invariant explicit and prevents silent data corruption via sentinel values.
    await session.execute(
        IVHistory.__table__.update()
        .where(
            IVHistory.__table__.c.instrument_id == instrument_id,
            IVHistory.__table__.c.date == target_date,
        )
        .values(
            rv_20d=Decimal(str(round(rv_20d, 6))),
            vrp=Decimal(str(round(vrp, 6))),
            vrp_regime=vrp_regime,
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def compute_vrp_for_date(
    target_date: date | None = None,
    *,
    dryrun_run_id=None,
) -> int:
    """Compute and persist VRP for all F&O instruments on target_date.

    MUST be called after iv_history_builder.build_for_date(target_date) has
    written atm_iv for the same date. If atm_iv is missing for an instrument,
    that instrument is silently skipped (no partial/corrupted row is written).

    Returns the count of rows updated.
    """
    if target_date is None:
        target_date = date.today()

    cfg = get_settings()
    lookback = cfg.fno_vrp_lookback_days
    min_pts = cfg.fno_vrp_min_data_points
    rich_thr = cfg.fno_vrp_rich_threshold
    cheap_thr = cfg.fno_vrp_cheap_threshold

    async with session_scope() as session:
        result = await session.execute(
            select(Instrument.id, Instrument.symbol).where(
                Instrument.is_fno == True,   # noqa: E712
                Instrument.is_active == True,
            )
        )
        instruments = result.all()

    updated = 0
    skipped_no_iv = 0
    skipped_no_price = 0

    for inst_id, symbol in instruments:
        try:
            async with session_scope() as session:
                atm_iv = await _get_atm_iv_for_date(session, str(inst_id), target_date)
                if atm_iv is None:
                    skipped_no_iv += 1
                    continue

                price_rows = await _get_price_rows(session, str(inst_id), target_date, lookback)

            if len(price_rows) < min_pts:
                logger.debug(
                    f"vrp_engine: {symbol} skipped — only {len(price_rows)} price rows "
                    f"(need ≥{min_pts})"
                )
                skipped_no_price += 1
                continue

            rv = yang_zhang_rv(price_rows)
            if rv is None:
                logger.debug(f"vrp_engine: {symbol} — YZ estimator returned None (insufficient pairs)")
                skipped_no_price += 1
                continue

            vrp_val = atm_iv - rv
            vrp_regime = classify_vrp_regime(vrp_val, rich_threshold=rich_thr, cheap_threshold=cheap_thr)

            async with session_scope() as session:
                await _upsert_vrp(session, str(inst_id), target_date, rv, vrp_val, vrp_regime)

            updated += 1
            logger.debug(
                f"vrp_engine: {symbol} atm_iv={atm_iv:.3f} rv={rv:.3f} "
                f"vrp={vrp_val:+.3f} regime={vrp_regime}"
            )

        except Exception as exc:
            logger.warning(f"vrp_engine: {symbol} failed: {exc!r}")

    logger.info(
        f"vrp_engine: {target_date} — updated={updated} "
        f"skipped_no_iv={skipped_no_iv} skipped_no_price={skipped_no_price}"
    )
    return updated


async def compute_vrp_for_date_range(start: date, end: date) -> int:
    """Backfill VRP for a historical date range (inclusive on both ends).

    Processes dates in ascending order. Dates where iv_history has no atm_iv
    rows (pre-deployment) are silently skipped. Returns total rows updated.
    """
    total = 0
    current = start
    while current <= end:
        n = await compute_vrp_for_date(current)
        total += n
        current += timedelta(days=1)
    logger.info(f"vrp_engine: backfill {start} → {end} complete — {total} total rows updated")
    return total


async def get_vrp_snapshot(
    instrument_id: str,
    as_of: date | None = None,
) -> VRPResult | None:
    """Return the most recent VRP reading for an instrument on or before as_of.

    Returns None when no VRP data exists (instrument too new, or
    price_daily was insufficient at the time the EOD pipeline ran).
    Callers must handle None gracefully — Phase 3 renders "(data unavailable)".
    """
    if as_of is None:
        as_of = date.today()

    async with session_scope() as session:
        result = await session.execute(
            select(
                IVHistory.date,
                IVHistory.atm_iv,
                IVHistory.rv_20d,
                IVHistory.vrp,
                IVHistory.vrp_regime,
            )
            .where(
                IVHistory.instrument_id == instrument_id,
                IVHistory.date <= as_of,
                IVHistory.vrp.isnot(None),
                IVHistory.dryrun_run_id.is_(None),
            )
            .order_by(IVHistory.date.desc())
            .limit(1)
        )
        row = result.first()

    if row is None:
        return None

    atm_iv_dec = _to_decimal_iv(float(row.atm_iv))
    if atm_iv_dec is None:
        return None

    # Fetch symbol for logging/display (lightweight — instruments are small table)
    async with session_scope() as session:
        sym_row = (await session.execute(
            select(Instrument.symbol).where(Instrument.id == instrument_id)
        )).scalar_one_or_none()

    return VRPResult(
        instrument_id=instrument_id,
        symbol=sym_row or "UNKNOWN",
        date=row.date,
        atm_iv=atm_iv_dec,
        rv_20d=float(row.rv_20d),
        vrp=float(row.vrp),
        vrp_regime=row.vrp_regime,
    )
