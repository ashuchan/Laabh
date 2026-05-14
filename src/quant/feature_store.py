"""Feature store — pulls per-tick features per (underlying, timestamp) from DB.

Data sources (live mode):
  * ``options_chain`` — per-strike rows. Each row carries ``underlying_ltp``;
    the ATM strike row supplies ``atm_iv``/``atm_oi``/``atm_bid``/``atm_ask``
    and the bid_qty/ask_qty inputs the OFI primitive needs (delta vs the
    prior snapshot).
  * ``vix_ticks`` — VIX value + regime closest at-or-before ``as_of``.
  * Underlying volume is not stored in ``options_chain``; live mode reports
    ``underlying_volume_3min = 0``. Primitives that key off volume gracefully
    skip when the average is zero (see ``ORBPrimitive._average_volume`` /
    ``MomentumPrimitive`` total-volume guard).

Returns ``None`` when the most-recent chain snapshot is older than 5 minutes,
so primitives can skip cleanly without raising.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytz
from loguru import logger
from sqlalchemy import text

from src.db import session_scope


# Stale-data cut-off: if the most-recent chain row is older than this we skip.
_MAX_CHAIN_AGE_SEC = 300  # 5 minutes
_IST = pytz.timezone("Asia/Kolkata")


@dataclass
class FeatureBundle:
    """All features needed by any primitive for one (underlying, tick)."""

    underlying_id: uuid.UUID
    underlying_symbol: str
    captured_at: datetime

    # Price / volume
    underlying_ltp: float
    underlying_volume_3min: float

    # VWAP / vol
    vwap_today: float
    realized_vol_3min: float   # annualised σ from last 3-min bar
    realized_vol_30min: float  # annualised σ from last 30-min window

    # Options chain (ATM)
    atm_iv: float
    atm_oi: float
    atm_bid: Decimal
    atm_ask: Decimal

    # Order-flow imbalance raw inputs (for OFI primitive)
    bid_volume_3min_change: float
    ask_volume_3min_change: float

    # Bollinger-band width (20-period on 3-min closes)
    bb_width: float

    # VIX
    vix_value: float
    vix_regime: str  # "low" | "normal" | "high" | "neutral"

    # Index-revert (only populated for index underlyings, else None)
    constituent_basket_value: float | None = None

    # Extra derived fields (filled in by get())
    session_start_ltp: float | None = None  # first-bar LTP for ORB
    orb_high: float | None = None           # 30-min ORB range high
    orb_low: float | None = None            # 30-min ORB range low


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ATM-per-snapshot SQL — exposed as a module constant so ensure_schema() can
# probe the exact statement the live read path runs.
_ATM_SNAPSHOTS_SQL = """
    WITH latest_snaps AS (
        SELECT DISTINCT oc.snapshot_at
        FROM options_chain oc
        WHERE oc.instrument_id = :uid
          AND oc.snapshot_at <= :ts
        ORDER BY oc.snapshot_at DESC
        LIMIT :n
    ),
    ranked AS (
        SELECT oc.snapshot_at, oc.underlying_ltp, oc.option_type,
               oc.strike_price, oc.iv, oc.oi,
               oc.bid_price, oc.ask_price, oc.bid_qty, oc.ask_qty,
               ROW_NUMBER() OVER (
                   PARTITION BY oc.snapshot_at, oc.option_type
                   ORDER BY ABS(oc.strike_price - oc.underlying_ltp), oc.expiry_date
               ) AS rk
        FROM options_chain oc
        INNER JOIN latest_snaps ls ON ls.snapshot_at = oc.snapshot_at
        WHERE oc.instrument_id = :uid
          AND oc.underlying_ltp IS NOT NULL
    )
    SELECT snapshot_at, underlying_ltp, option_type,
           iv, oi, bid_price, ask_price, bid_qty, ask_qty
    FROM ranked
    WHERE rk = 1
    ORDER BY snapshot_at ASC, option_type
"""

_VIX_LATEST_SQL = """
    SELECT vix_value, regime
    FROM vix_ticks
    WHERE timestamp <= :ts
    ORDER BY timestamp DESC
    LIMIT 1
"""

_ORB_RANGE_SQL = """
    SELECT DISTINCT oc.snapshot_at, oc.underlying_ltp
    FROM options_chain oc
    WHERE oc.instrument_id = :uid
      AND oc.snapshot_at >= :open
      AND oc.snapshot_at <= :end
      AND oc.underlying_ltp IS NOT NULL
    ORDER BY oc.snapshot_at ASC
"""

_SYMBOL_LOOKUP_SQL = "SELECT symbol FROM instruments WHERE id = :uid"


async def get(
    underlying_id: uuid.UUID,
    as_of: datetime,
    *,
    history_rows: int = 30,
) -> FeatureBundle | None:
    """Return a FeatureBundle for *underlying_id* at *as_of*, or None if stale.

    Args:
        underlying_id: The instrument UUID.
        as_of: Reference timestamp (UTC). The most-recent chain snapshot at
            or before this time anchors the bundle.
        history_rows: How many prior chain snapshots to load for vol / BB /
            VWAP calculation. 30 ≈ 90 minutes of 3-min snapshots.
    """
    async with session_scope() as session:
        rows = (await session.execute(
            text(_ATM_SNAPSHOTS_SQL),
            {"uid": underlying_id, "ts": as_of, "n": history_rows},
        )).all()
        if not rows:
            return None

        sym_row = (await session.execute(
            text(_SYMBOL_LOOKUP_SQL), {"uid": underlying_id}
        )).one_or_none()
        if sym_row is None:
            return None
        symbol = sym_row.symbol

        # Pivot rows into per-snapshot (CE, PE) buckets, keeping underlying_ltp once.
        per_snap: dict[Any, dict[str, Any]] = {}
        for r in rows:
            bucket = per_snap.setdefault(
                r.snapshot_at,
                {"ltp": float(r.underlying_ltp or 0)},
            )
            bucket[r.option_type] = r

        ordered = sorted(per_snap.items(), key=lambda kv: kv[0])
        current_ts, current_bucket = ordered[-1]

        age = (
            as_of.replace(tzinfo=timezone.utc)
            - current_ts.replace(tzinfo=timezone.utc)
        ).total_seconds()
        if age > _MAX_CHAIN_AGE_SEC:
            logger.debug(
                f"feature_store: chain stale for {symbol}: "
                f"{age:.0f}s > {_MAX_CHAIN_AGE_SEC}s"
            )
            return None

        atm = current_bucket.get("CE") or current_bucket.get("PE")
        if atm is None:
            return None

        ltps = [v["ltp"] for _, v in ordered]
        # options_chain does not carry underlying spot volume; fall back to 0.
        # Primitives that key off volume tolerate zero (ORB: vol_ok=True when
        # avg_vol=0; momentum: short-circuits when total_vol=0).
        vols = [0.0] * len(ltps)

        bid_change = 0.0
        ask_change = 0.0
        if len(ordered) >= 2:
            _prev_ts, prev_bucket = ordered[-2]
            prev_atm = prev_bucket.get("CE") or prev_bucket.get("PE")
            if prev_atm is not None:
                bid_change = float((atm.bid_qty or 0) - (prev_atm.bid_qty or 0))
                ask_change = float((atm.ask_qty or 0) - (prev_atm.ask_qty or 0))

        vwap = _compute_vwap(ltps, vols)
        rv_3 = _realized_vol(ltps[-2:] if len(ltps) >= 2 else ltps)
        rv_30 = _realized_vol(ltps[-10:] if len(ltps) >= 10 else ltps)
        bb_width = _bb_width(ltps[-20:] if len(ltps) >= 20 else ltps)

        vix_val, vix_regime = await _fetch_vix(session, as_of)
        orb_high, orb_low, session_start_ltp = await _fetch_orb_range(
            session, underlying_id, as_of
        )

        return FeatureBundle(
            underlying_id=underlying_id,
            underlying_symbol=symbol,
            captured_at=current_ts,
            underlying_ltp=float(atm.underlying_ltp or 0),
            underlying_volume_3min=0.0,
            vwap_today=vwap,
            realized_vol_3min=rv_3,
            realized_vol_30min=rv_30,
            atm_iv=float(atm.iv or 0),
            atm_oi=float(atm.oi or 0),
            atm_bid=Decimal(str(atm.bid_price or 0)),
            atm_ask=Decimal(str(atm.ask_price or 0)),
            bid_volume_3min_change=bid_change,
            ask_volume_3min_change=ask_change,
            bb_width=bb_width,
            vix_value=vix_val,
            vix_regime=vix_regime,
            session_start_ltp=session_start_ltp,
            orb_high=orb_high,
            orb_low=orb_low,
        )


async def ensure_schema() -> None:
    """Probe the SQL statements the live read path runs. Raises on column drift.

    Runs each parameterised query with a sentinel all-zero UUID — the queries
    return empty rows but PostgreSQL still validates every column reference.
    Any ``UndefinedColumn`` / ``UndefinedTable`` error is re-raised wrapped
    in ``RuntimeError`` with a clear message. Call once at orchestrator
    startup so a schema regression aborts the loop loudly instead of silently
    swallowing every tick.
    """
    sentinel_uid = uuid.UUID("00000000-0000-0000-0000-000000000000")
    now = datetime.now(tz=timezone.utc)
    probes: list[tuple[str, str, dict]] = [
        (
            "options_chain ATM snapshots CTE",
            _ATM_SNAPSHOTS_SQL,
            {"uid": sentinel_uid, "ts": now, "n": 1},
        ),
        ("vix_ticks latest", _VIX_LATEST_SQL, {"ts": now}),
        (
            "options_chain ORB range",
            _ORB_RANGE_SQL,
            {"uid": sentinel_uid, "open": now, "end": now},
        ),
        ("instruments symbol lookup", _SYMBOL_LOOKUP_SQL, {"uid": sentinel_uid}),
    ]
    async with session_scope() as session:
        for label, sql, params in probes:
            try:
                await session.execute(text(sql), params)
            except Exception as exc:
                raise RuntimeError(
                    f"feature_store.ensure_schema: probe {label!r} failed: {exc!r}. "
                    "The live feature_store SQL references columns missing from "
                    "the current schema — fix before the live loop runs."
                ) from exc


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_vwap(ltps: list[float], vols: list[float]) -> float:
    """Volume-weighted average price over the supplied window.

    Note: this is a *rolling-window* VWAP across the bars passed in (the
    feature store loads the most-recent `history_rows` bars), not a strict
    session-cumulative VWAP. Primitives that need session-VWAP semantics
    should treat this as an approximation valid once enough warmup has
    elapsed that the window covers the session.

    When all volumes are zero (live mode, no spot-volume source), falls
    back to a simple arithmetic mean of LTPs so the primitives still have
    a usable VWAP anchor.
    """
    total_vol = sum(vols)
    if not ltps:
        return 0.0
    if total_vol == 0:
        return sum(ltps) / len(ltps)
    return sum(p * v for p, v in zip(ltps, vols)) / total_vol


def _realized_vol(ltps: list[float], *, bars_per_year: int = 26040) -> float:
    """Annualised realised volatility from a sequence of LTPs.

    Uses log-returns on consecutive prices.
    bars_per_year = 252 trading days × ~103.33 3-min bars/day ≈ 26040.
    """
    if len(ltps) < 2:
        return 0.0
    log_rets = [
        math.log(ltps[i] / ltps[i - 1])
        for i in range(1, len(ltps))
        if ltps[i - 1] > 0 and ltps[i] > 0
    ]
    if not log_rets:
        return 0.0
    n = len(log_rets)
    mean = sum(log_rets) / n
    var = sum((r - mean) ** 2 for r in log_rets) / max(n - 1, 1)
    return math.sqrt(var * bars_per_year)


def _bb_width(ltps: list[float]) -> float:
    """Bollinger Band width = (upper - lower) / middle for the given window."""
    if len(ltps) < 2:
        return 0.0
    n = len(ltps)
    mean = sum(ltps) / n
    std = math.sqrt(sum((p - mean) ** 2 for p in ltps) / max(n - 1, 1))
    if mean == 0:
        return 0.0
    return (4 * std) / mean  # (upper-lower)/middle = 4σ/mean (2σ bands)


async def _fetch_vix(session, as_of: datetime) -> tuple[float, str]:
    """Return (vix_value, regime) from vix_ticks closest to ``as_of``."""
    from src.config import get_settings

    row = (await session.execute(text(_VIX_LATEST_SQL), {"ts": as_of})).one_or_none()
    if row is not None:
        return float(row.vix_value), str(row.regime)

    # Fallback: no tick yet — use a sensible default and compute regime from
    # current settings thresholds so callers always receive a string.
    settings = get_settings()
    vix = 15.0
    if vix < settings.fno_vix_low_threshold:
        regime = "low"
    elif vix > settings.fno_vix_high_threshold:
        regime = "high"
    else:
        regime = "neutral"
    return vix, regime


async def _fetch_orb_range(
    session,
    underlying_id: uuid.UUID,
    as_of: datetime,
) -> tuple[float | None, float | None, float | None]:
    """Return (orb_high, orb_low, session_start_ltp) from 09:15–09:45 IST bars.

    Only the portion of the ORB window that has elapsed by ``as_of`` is read,
    so morning ticks before 09:45 return a partial range (primitives treat
    None as warmup-not-complete).
    """
    ist_as_of = (
        as_of.astimezone(_IST)
        if as_of.tzinfo is not None
        else _IST.localize(as_of)
    )
    session_open = _IST.localize(
        datetime.combine(
            ist_as_of.date(),
            datetime.min.time().replace(hour=9, minute=15),
        )
    ).astimezone(timezone.utc)
    orb_end = session_open + timedelta(minutes=30)
    cap = min(as_of, orb_end)
    if cap < session_open:
        return None, None, None

    rows = (await session.execute(
        text(_ORB_RANGE_SQL),
        {"uid": underlying_id, "open": session_open, "end": cap},
    )).all()
    if not rows:
        return None, None, None
    ltps = [float(r.underlying_ltp) for r in rows if r.underlying_ltp is not None]
    if not ltps:
        return None, None, None
    return max(ltps), min(ltps), ltps[0]
