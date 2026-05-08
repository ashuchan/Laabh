"""Market sentiment collector — writes raw_content[media_type='sentiment'].

Phase 2 catalyst scoring reads this row to derive its sentiment_score
component. We synthesize a 0-10 score from four independent dimensions:

    1. VIX             — India VIX from vix_ticks (yfinance fallback when stale)
    2. trend_1d        — NIFTY 50 close vs prior close + breadth leg (% F&O up)
    3. trend_1w        — NIFTY 50 close vs ~5 trading days ago + breadth
    4. trend_1m        — NIFTY 50 close vs ~21 trading days ago + breadth

Per-horizon score = avg(index_leg, breadth_leg) where each leg is on 0-10.
Final score = weighted average of available components, renormalised so the
weights of any dropped components don't compress the score toward neutral.

The 1-day component's weight is automatically halved when the most recent
trading day is more than 1 calendar day behind today (Mondays + post-holiday).
The 1d signal is fresher mid-week than after a weekend.

Persisted JSON shape::

    {
      "score": 6.03,
      "degraded": false,
      "trading_day_gap": 1,
      "weights_applied": { ... },
      "components": {
        "vix":      { "value": 16.68, "regime": "neutral", "score": 5.5 },
        "trend_1d": { "nifty_pct": 0.42, "breadth_pct": 53, "score": 5.65 },
        "trend_1w": { "score": null, "reason": "..." },
        "trend_1m": { "nifty_pct": 2.34, "breadth_pct": 61, "score": 6.13 }
      }
    }

Phase 2 reads only ``score``; everything else is for audit / dashboards.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from src.config import get_settings
from src.db import session_scope
from src.fno.vix_collector import _fetch_vix_historical, classify_regime, latest_vix
from src.models.content import RawContent
from src.models.fno_vix import VIXTick
from src.models.instrument import Instrument
from src.models.price import PriceDaily

_settings = get_settings()

_SOURCE_NAME = "Market Sentiment (derived)"

# Horizon trading-day windows used by the trend legs. NSE has ~21 trading
# days per calendar month and ~5 per week. These are intentionally
# integer-valued — we snap to the closest available row in price_daily.
_HORIZONS: dict[str, int] = {
    "trend_1d": 1,
    "trend_1w": 5,
    "trend_1m": 21,
}

# Per-horizon scale converting NIFTY pct_change to a 0-10 score around the
# neutral 5.0. Calibrated so that a "very strong" move in either direction
# saturates the 0/10 bound: ±2% intraday, ±5% in a week, ±10% in a month.
_INDEX_LEG_SCALE: dict[str, float] = {
    "trend_1d": 2.5,   # 5 + (+2.0 * 2.5) = 10
    "trend_1w": 1.0,   # 5 + (+5.0 * 1.0) = 10
    "trend_1m": 0.5,   # 5 + (+10.0 * 0.5) = 10
}


# ---------------------------------------------------------------------------
# Pure scoring helpers (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def score_vix(vix_value: float) -> float:
    """Map VIX to a 0-10 sentiment score. Inversion is intentional: low VIX
    = complacency = bullish bias; high VIX = fear = bearish bias.

    Anchors (saturating piecewise linear): 12 → 8.0, 15 → 6.0, 18 → 4.0,
    22 → 2.0, ≥30 → 0.5. ≤12 saturates at 8.0 (we never call low-VIX
    "extremely bullish" — there's no fear of fear, but also no informational
    edge in calm markets).
    """
    anchors = [(12.0, 8.0), (15.0, 6.0), (18.0, 4.0), (22.0, 2.0), (30.0, 0.5)]
    if vix_value <= anchors[0][0]:
        return anchors[0][1]
    if vix_value >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= vix_value <= x1:
            t = (vix_value - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 2)
    return 5.0  # unreachable — defensive


def score_index_leg(pct_change: float, scale: float) -> float:
    """Linear: 5 + pct_change * scale, clipped to [0, 10]."""
    return round(max(0.0, min(10.0, 5.0 + pct_change * scale)), 2)


def score_breadth_leg(pct_up: float) -> float:
    """Linear: pct_up (0-100) → score (0-10)."""
    return round(max(0.0, min(10.0, pct_up / 10.0)), 2)


def combine_horizon(
    index_score: float | None,
    breadth_score: float | None,
) -> float | None:
    """Average index + breadth legs. If only one is present, return it; if
    both None, return None (the whole horizon drops out)."""
    if index_score is None and breadth_score is None:
        return None
    if index_score is None:
        return breadth_score
    if breadth_score is None:
        return index_score
    return round((index_score + breadth_score) / 2, 2)


def decayed_weights(
    base: dict[str, float],
    *,
    trading_day_gap: int,
    stale_1d_decay: float,
) -> dict[str, float]:
    """Apply the post-weekend / post-holiday decay to the 1-day weight, then
    renormalise so weights sum to 1.0."""
    w = dict(base)
    if trading_day_gap > 1 and "trend_1d" in w:
        w["trend_1d"] *= stale_1d_decay
    total = sum(w.values())
    if total <= 0:
        return w
    return {k: round(v / total, 4) for k, v in w.items()}


def combine_score(
    weights: dict[str, float],
    scores: dict[str, float | None],
) -> tuple[float | None, dict[str, float]]:
    """Weighted average over components whose score is not None.

    Returns ``(final_score_or_None, weights_actually_applied)``. The
    returned weights are renormalised across only the present components,
    so a single-component result still hits the full 0-10 range.
    """
    present = {
        k: (weights.get(k, 0.0), v)
        for k, v in scores.items()
        if v is not None and weights.get(k, 0.0) > 0
    }
    if not present:
        return None, {}
    total_w = sum(w for w, _ in present.values())
    applied = {k: round(w / total_w, 4) for k, (w, _) in present.items()}
    final = sum(s * w / total_w for w, s in present.values())
    return round(final, 2), applied


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

async def _ensure_data_source() -> uuid.UUID:
    """Look up or create the sentiment data_source row, returning its id.

    Idempotent: safe to call on every run. Created lazily so a fresh DB
    install (with seed.sql not re-run) doesn't break this collector.
    """
    from src.models.source import DataSource

    async with session_scope() as session:
        existing = (await session.execute(
            select(DataSource).where(DataSource.name == _SOURCE_NAME)
        )).scalar_one_or_none()
        if existing is not None:
            return existing.id

        new_row = DataSource(
            name=_SOURCE_NAME,
            # 'api_feed' is the closest fit in the source_type enum for a
            # synthesized/derived feed — there's no 'derived' variant, and
            # this is what macro_collector + fii_dii_collector use.
            type="api_feed",
            config={"description": "Market sentiment synthesized from VIX + NIFTY 50 + breadth"},
            poll_interval_sec=300,
            priority=5,
            extraction_schema={},
        )
        session.add(new_row)
        await session.flush()
        sid = new_row.id
        await session.commit()
        logger.info(f"sentiment_collector: created data_sources row {sid}")
        return sid


async def _fetch_vix_component(*, as_of: datetime | None) -> dict[str, Any]:
    """Pull the latest VIX value, falling back to yfinance if stale or empty.

    Returns a component dict::
        {"value": 16.68, "regime": "neutral", "score": 5.5}
    or, on hard failure::
        {"score": null, "reason": "..."}
    """
    s = _settings
    now = as_of or datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(hours=s.fno_sentiment_vix_max_stale_hours)

    tick = await latest_vix()
    if tick is not None and tick.timestamp >= cutoff:
        score = score_vix(float(tick.vix_value))
        return {
            "value": float(tick.vix_value),
            "regime": tick.regime,
            "score": score,
            "source": "vix_ticks",
            "ts": tick.timestamp.isoformat(),
        }

    # Stale or missing → try yfinance historical
    try:
        vix_value = await _fetch_vix_historical(now)
        return {
            "value": float(vix_value),
            "regime": classify_regime(float(vix_value)),
            "score": score_vix(float(vix_value)),
            "source": "yfinance_fallback",
            "ts": now.isoformat(),
        }
    except Exception as exc:
        msg = f"vix unavailable (latest tick {'none' if tick is None else f'>{s.fno_sentiment_vix_max_stale_hours}h stale'}, yfinance: {exc})"
        logger.warning(f"sentiment_collector: {msg}")
        return {"score": None, "reason": msg}


async def _resolve_index_id(symbol: str) -> uuid.UUID | None:
    async with session_scope() as session:
        row = (await session.execute(
            select(Instrument.id).where(Instrument.symbol == symbol).limit(1)
        )).scalar_one_or_none()
    return row


@dataclass
class _PriceWindow:
    latest_date: date | None
    latest_close: float | None
    prior_date: date | None
    prior_close: float | None


async def _index_price_window(index_id: uuid.UUID, horizon_days: int) -> _PriceWindow:
    """Get latest + N-trading-days-prior closes for a single instrument.

    "N trading days prior" = the row at index `[N]` from latest in date-desc
    order. We don't try to compensate for missing trading days mid-window —
    if the user has a contiguous price_daily series (which the EOD job
    guarantees), this gives exactly the right snap.
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(PriceDaily.date, PriceDaily.close)
            .where(
                PriceDaily.instrument_id == index_id,
                PriceDaily.close.isnot(None),  # skip null-close holiday rows
            )
            .order_by(PriceDaily.date.desc())
            .limit(horizon_days + 1)
        )).all()

    if not rows:
        return _PriceWindow(None, None, None, None)
    latest = rows[0]
    if len(rows) <= horizon_days:
        return _PriceWindow(latest.date, float(latest.close), None, None)
    prior = rows[horizon_days]
    return _PriceWindow(
        latest_date=latest.date,
        latest_close=float(latest.close),
        prior_date=prior.date,
        prior_close=float(prior.close),
    )


async def _breadth_for_horizon(horizon_days: int) -> tuple[int, int]:
    """Return (count_with_data, count_up) across all is_fno=true instruments.

    "up" means latest close > N-rows-back close in price_daily. Done in one
    SQL pass via a window function for efficiency — alternative is a
    Python loop over ~215 instruments which would issue 430 queries.
    """
    from sqlalchemy import text

    # The inner CTE filters `pd.close IS NOT NULL` BEFORE the row-number is
    # assigned. Without this filter, holiday rows with null close would
    # consume a rank position (e.g., rn=2) but contribute null data — and
    # the outer window's `MAX(close) FILTER (WHERE rn = 2)` would silently
    # return NULL for that instrument, dropping it from the breadth count
    # even though it has plenty of valid history.
    sql = text("""
        WITH ranked AS (
            SELECT
                pd.instrument_id,
                pd.date,
                pd.close,
                ROW_NUMBER() OVER (PARTITION BY pd.instrument_id
                                   ORDER BY pd.date DESC) AS rn
            FROM price_daily pd
            JOIN instruments i ON i.id = pd.instrument_id
            WHERE i.is_fno = true
              AND i.is_active = true
              AND pd.close IS NOT NULL
        ),
        windowed AS (
            SELECT
                instrument_id,
                MAX(close) FILTER (WHERE rn = 1)                 AS latest_close,
                MAX(close) FILTER (WHERE rn = :horizon_plus_one) AS prior_close
            FROM ranked
            WHERE rn IN (1, :horizon_plus_one)
            GROUP BY instrument_id
        )
        SELECT
            COUNT(*)                                           AS n_with_data,
            COUNT(*) FILTER (WHERE latest_close > prior_close) AS n_up
        FROM windowed
        WHERE latest_close IS NOT NULL AND prior_close IS NOT NULL
    """)
    async with session_scope() as session:
        row = (await session.execute(
            sql, {"horizon_plus_one": horizon_days + 1}
        )).one()
    return int(row.n_with_data or 0), int(row.n_up or 0)


async def _trend_component(
    name: str,
    horizon_days: int,
    *,
    index_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Build one trend horizon's component dict (1d / 1w / 1m)."""
    s = _settings

    # Index leg
    index_leg: float | None = None
    nifty_pct: float | None = None
    if index_id is not None:
        win = await _index_price_window(index_id, horizon_days)
        if win.latest_close is not None and win.prior_close not in (None, 0):
            nifty_pct = round(
                (win.latest_close - win.prior_close) / win.prior_close * 100, 4
            )
            index_leg = score_index_leg(nifty_pct, _INDEX_LEG_SCALE[name])

    # Breadth leg
    n_with_data, n_up = await _breadth_for_horizon(horizon_days)
    min_n = s.fno_sentiment_min_breadth_instruments
    if n_with_data >= min_n:
        breadth_pct = round(100.0 * n_up / n_with_data, 2)
        breadth_leg = score_breadth_leg(breadth_pct)
    else:
        breadth_pct = None
        breadth_leg = None

    score = combine_horizon(index_leg, breadth_leg)
    out: dict[str, Any] = {
        "score": score,
        "nifty_pct": nifty_pct,
        "breadth_pct": breadth_pct,
        "index_leg": index_leg,
        "breadth_leg": breadth_leg,
        "instruments_with_data": n_with_data,
    }
    if score is None:
        reasons = []
        if index_leg is None:
            reasons.append(
                "no NIFTY index price history" if index_id is None
                else f"NIFTY price_daily lacks t-{horizon_days} row"
            )
        if breadth_leg is None:
            reasons.append(
                f"insufficient breadth coverage ({n_with_data} instruments < min {min_n})"
            )
        out["reason"] = "; ".join(reasons) or "unknown"
    return out


def _trading_day_gap(latest_trading_date: date | None, today: date) -> int:
    """Calendar-day delta between today and the latest available trading date.

    1 on a normal day, 3 on Monday after a weekend, more after holidays.
    Defaults to 1 when latest_trading_date is unknown so we don't penalize
    on missing data.
    """
    if latest_trading_date is None:
        return 1
    return max(1, (today - latest_trading_date).days)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def compute_sentiment(
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,  # noqa: ARG001 — convention
) -> dict[str, Any]:
    """Build the sentiment payload for a single point in time. Pure-ish:
    reads from DB but does not write."""
    s = _settings
    now = as_of or datetime.now(tz=timezone.utc)
    today = now.date()

    # Resolve index id once (None if not seeded)
    index_id = await _resolve_index_id(s.fno_sentiment_index_symbol)

    # 1. VIX
    vix_comp = await _fetch_vix_component(as_of=as_of)

    # 2-4. Trend horizons
    trend_components: dict[str, dict[str, Any]] = {}
    latest_index_date: date | None = None
    for name, days in _HORIZONS.items():
        comp = await _trend_component(name, days, index_id=index_id)
        trend_components[name] = comp
        # Capture the latest NIFTY trading date for the 1d-decay calc
        if name == "trend_1d" and index_id is not None:
            win = await _index_price_window(index_id, days)
            latest_index_date = win.latest_date

    gap = _trading_day_gap(latest_index_date, today)

    # Weighted aggregate
    base_weights = {
        "vix": s.fno_sentiment_weight_vix,
        "trend_1d": s.fno_sentiment_weight_1d,
        "trend_1w": s.fno_sentiment_weight_1w,
        "trend_1m": s.fno_sentiment_weight_1m,
    }
    weights_after_decay = decayed_weights(
        base_weights,
        trading_day_gap=gap,
        stale_1d_decay=s.fno_sentiment_stale_1d_decay,
    )
    component_scores: dict[str, float | None] = {
        "vix": vix_comp.get("score"),
        "trend_1d": trend_components["trend_1d"].get("score"),
        "trend_1w": trend_components["trend_1w"].get("score"),
        "trend_1m": trend_components["trend_1m"].get("score"),
    }
    final_score, weights_applied = combine_score(weights_after_decay, component_scores)

    degraded = final_score is None
    if degraded:
        logger.warning(
            "sentiment_collector: every component degraded — emitting neutral 5.0"
        )
        final_score = 5.0

    return {
        "score": final_score,
        "degraded": degraded,
        "trading_day_gap": gap,
        "as_of": now.isoformat(),
        "weights_applied": weights_applied,
        "components": {
            "vix": vix_comp,
            "trend_1d": trend_components["trend_1d"],
            "trend_1w": trend_components["trend_1w"],
            "trend_1m": trend_components["trend_1m"],
        },
    }


async def run_once(
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Compute sentiment and persist it as a raw_content row.

    Returns the payload that was written.
    """
    payload = await compute_sentiment(as_of=as_of, dryrun_run_id=dryrun_run_id)
    source_id = await _ensure_data_source()
    stamp = as_of if as_of is not None else datetime.now(tz=timezone.utc)

    content_text = json.dumps(payload, default=str)
    h = hashlib.sha256(
        f"sentiment:{stamp.isoformat()}".encode()
    ).hexdigest()

    async with session_scope() as session:
        session.add(RawContent(
            source_id=source_id,
            content_hash=h,
            title=f"Sentiment: {payload['score']:.2f}"
                  f"{' (degraded)' if payload['degraded'] else ''}",
            content_text=content_text,
            media_type="sentiment",
            is_processed=True,
            fetched_at=stamp,
            dryrun_run_id=dryrun_run_id,
        ))
        await session.commit()

    logger.info(
        f"sentiment_collector: score={payload['score']:.2f} "
        f"(degraded={payload['degraded']}, gap={payload['trading_day_gap']}d)"
    )
    return payload
