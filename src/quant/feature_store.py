"""Feature store — pulls 3-min features per (underlying, timestamp) from DB.

All reads are async. Returns None when data is stale (>5 min) so callers can
gracefully skip the underlying for that tick.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from loguru import logger
from sqlalchemy import select, text

from src.db import session_scope


# Stale-data cut-off: if the most-recent chain row is older than this we skip.
_MAX_CHAIN_AGE_SEC = 300  # 5 minutes


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
    vix_regime: str  # "low" | "normal" | "high"

    # Index-revert (only populated for index underlyings, else None)
    constituent_basket_value: float | None = None

    # Extra derived fields (filled in by get())
    session_start_ltp: float | None = None  # first-bar LTP for ORB
    orb_high: float | None = None           # 30-min ORB range high
    orb_low: float | None = None            # 30-min ORB range low


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get(
    underlying_id: uuid.UUID,
    as_of: datetime,
    *,
    history_rows: int = 30,
) -> FeatureBundle | None:
    """Return a FeatureBundle for *underlying_id* at *as_of*, or None if stale.

    Args:
        underlying_id: The instrument UUID.
        as_of: Reference timestamp (UTC). Pulled from the most-recent chain
            snapshot at or before this time.
        history_rows: How many prior 3-min bars to load for vol / BB calc.
    """
    async with session_scope() as session:
        # --- 1. Freshness check ---
        chain_q = text("""
            SELECT oc.captured_at, oc.underlying_ltp, oc.underlying_volume,
                   oc.atm_iv, oc.atm_oi, oc.atm_bid, oc.atm_ask,
                   oc.bid_qty_change_3min, oc.ask_qty_change_3min,
                   i.symbol
            FROM options_chain oc
            JOIN instruments i ON i.id = oc.underlying_id
            WHERE oc.underlying_id = :uid
              AND oc.captured_at <= :ts
            ORDER BY oc.captured_at DESC
            LIMIT 1
        """)
        row = (await session.execute(chain_q, {"uid": underlying_id, "ts": as_of})).one_or_none()
        if row is None:
            return None

        age = (as_of.replace(tzinfo=timezone.utc) - row.captured_at.replace(tzinfo=timezone.utc)).total_seconds()
        if age > _MAX_CHAIN_AGE_SEC:
            logger.debug(f"chain stale for {row.symbol}: {age:.0f}s > {_MAX_CHAIN_AGE_SEC}s")
            return None

        underlying_ltp: float = float(row.underlying_ltp or 0)
        underlying_volume: float = float(row.underlying_volume or 0)

        # --- 2. Historical bars for vol / BB / VWAP ---
        hist_q = text("""
            SELECT oc.captured_at, oc.underlying_ltp, oc.underlying_volume
            FROM options_chain oc
            WHERE oc.underlying_id = :uid
              AND oc.captured_at <= :ts
            ORDER BY oc.captured_at DESC
            LIMIT :n
        """)
        hist_rows = (await session.execute(
            hist_q, {"uid": underlying_id, "ts": as_of, "n": history_rows}
        )).all()
        ltps = [float(r.underlying_ltp or 0) for r in reversed(hist_rows)]
        vols = [float(r.underlying_volume or 0) for r in reversed(hist_rows)]

        # --- 3. VWAP (cumulative since session open) ---
        vwap = _compute_vwap(ltps, vols)

        # --- 4. Realized vol (3-min and 30-min) ---
        rv_3 = _realized_vol(ltps[-2:] if len(ltps) >= 2 else ltps, bars_per_year=26040)
        rv_30 = _realized_vol(ltps[-10:] if len(ltps) >= 10 else ltps, bars_per_year=26040)

        # --- 5. Bollinger-band width on last 20 1-min closes ---
        bb_width = _bb_width(ltps[-20:] if len(ltps) >= 20 else ltps)

        # --- 6. VIX ---
        vix_val, vix_regime = await _fetch_vix(session, as_of)

        # --- 7. ORB range (first 30 min of session) ---
        session_date = as_of.date()
        orb_high, orb_low, session_start_ltp = await _fetch_orb_range(
            session, underlying_id, session_date
        )

        return FeatureBundle(
            underlying_id=underlying_id,
            underlying_symbol=row.symbol,
            captured_at=row.captured_at,
            underlying_ltp=underlying_ltp,
            underlying_volume_3min=underlying_volume,
            vwap_today=vwap,
            realized_vol_3min=rv_3,
            realized_vol_30min=rv_30,
            atm_iv=float(row.atm_iv or 0),
            atm_oi=float(row.atm_oi or 0),
            atm_bid=Decimal(str(row.atm_bid or 0)),
            atm_ask=Decimal(str(row.atm_ask or 0)),
            bid_volume_3min_change=float(row.bid_qty_change_3min or 0),
            ask_volume_3min_change=float(row.ask_qty_change_3min or 0),
            bb_width=bb_width,
            vix_value=vix_val,
            vix_regime=vix_regime,
            session_start_ltp=session_start_ltp,
            orb_high=orb_high,
            orb_low=orb_low,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_vwap(ltps: list[float], vols: list[float]) -> float:
    """Volume-weighted average price over available bars."""
    total_vol = sum(vols)
    if total_vol == 0 or not ltps:
        return ltps[-1] if ltps else 0.0
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
    """Return (vix_value, regime) from vix_ticks closest to as_of."""
    from src.config import get_settings

    q = text("""
        SELECT value FROM vix_ticks
        WHERE captured_at <= :ts
        ORDER BY captured_at DESC LIMIT 1
    """)
    row = (await session.execute(q, {"ts": as_of})).one_or_none()
    vix = float(row.value) if row else 15.0

    settings = get_settings()
    if vix < settings.fno_vix_low_threshold:
        regime = "low"
    elif vix > settings.fno_vix_high_threshold:
        regime = "high"
    else:
        regime = "normal"
    return vix, regime


async def _fetch_orb_range(
    session,
    underlying_id: uuid.UUID,
    session_date,
) -> tuple[float | None, float | None, float | None]:
    """Return (orb_high, orb_low, session_start_ltp) from 09:15–09:45 IST bars."""
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    session_open = ist.localize(
        datetime.combine(session_date, datetime.min.time().replace(hour=9, minute=15))
    ).astimezone(timezone.utc)
    orb_end = session_open + timedelta(minutes=30)

    q = text("""
        SELECT underlying_ltp, captured_at
        FROM options_chain
        WHERE underlying_id = :uid
          AND captured_at >= :open AND captured_at <= :orb_end
        ORDER BY captured_at ASC
    """)
    rows = (await session.execute(
        q, {"uid": underlying_id, "open": session_open, "orb_end": orb_end}
    )).all()
    if not rows:
        return None, None, None
    ltps = [float(r.underlying_ltp or 0) for r in rows if r.underlying_ltp]
    if not ltps:
        return None, None, None
    return max(ltps), min(ltps), ltps[0]
