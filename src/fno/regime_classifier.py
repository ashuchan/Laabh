"""Market Regime Classifier — rules-based daily regime detection.

Classifies the current market into one of six regimes using quantitative
thresholds. This is a rules-based implementation; it can be upgraded to an
HMM (Hamilton 1989) or ML classifier once sufficient labeled history accumulates.

Regimes and strategy implications:
  vol_expansion    — VIX rising rapidly, IV not yet catching realized vol.
                     IV buying is cheap; long straddles/strangles have positive EV.
                     Avoid selling premium.

  vol_contraction  — VIX falling fast after a spike, IV rich vs realized vol.
                     Premium-selling harvest window: iron condors, calendars.

  range_high_iv    — VIX elevated but spot not trending strongly.
                     DII absorption creates a range. Classic condor/strangle territory.

  trending_bear    — Consistent downtrend: spot down multi-day, breadth collapsed.
                     Bear put spreads and bear call spreads; avoid long delta.

  trending_bull    — Consistent uptrend: spot up multi-day, breadth elevated.
                     Bull call spreads and short puts; avoid short delta.

  neutral          — No strong regime signal. Evaluate stock-by-stock.

Classification priority (checked in order — FIRST match wins):
  1. vol_expansion   (VIX spike + IV cheap vs RV)
  2. vol_contraction (VIX falling + IV rich)
  3. trending_bear   (consistent multi-day downtrend + breadth collapse)
  4. trending_bull   (consistent multi-day uptrend + breadth expansion)
  5. range_high_iv   (high VIX but no strong trend)
  6. neutral         (default)

Research basis:
  Hamilton (1989), "A New Approach to the Economic Analysis of Nonstationary
  Time Series" — Markov switching model framework (used here as inspiration
  for regime labels; actual classification is rules-based).
  Ang & Bekaert (2002), "Regime Switches in Interest Rates" — regime taxonomy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select, text

from src.config import get_settings
from src.db import session_scope
from src.models.instrument import Instrument


# ---------------------------------------------------------------------------
# Strategy playbooks per regime
# ---------------------------------------------------------------------------

STRATEGY_PLAYBOOKS: dict[str, list[str]] = {
    "vol_expansion":   ["long_straddle", "long_strangle", "bull_call_spread", "bear_put_spread"],
    "vol_contraction": ["iron_condor", "short_strangle", "calendar_spread", "bull_put_spread", "bear_call_spread"],
    "range_high_iv":   ["iron_condor", "short_strangle", "bear_call_spread", "bull_put_spread"],
    "trending_bear":   ["bear_put_spread", "bear_call_spread", "long_put"],
    "trending_bull":   ["bull_call_spread", "bull_put_spread", "long_call"],
    "neutral":         ["iron_condor", "bull_call_spread", "bear_put_spread"],
}

# Phase 2 regime_bias to apply when this regime is active.
# Additive to any existing policy_event bias; capped upstream.
REGIME_BIAS: dict[str, float] = {
    "vol_expansion":   0.0,   # no directional bias; let per-stock signals dominate
    "vol_contraction": 0.3,   # slight bullish tilt as fear subsides
    "range_high_iv":   0.0,
    "trending_bear":  -1.0,   # all composites suppressed; bidirectional gate still finds bearish names
    "trending_bull":   1.0,
    "neutral":         0.0,
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RegimeResult:
    run_date: date
    regime: str
    confidence: float
    strategy_playbook: list[str]

    # Raw inputs (for audit log and prompt block)
    vix_current: float | None = None
    vix_prev_close: float | None = None
    vix_velocity: float | None = None       # (current - prev) / prev  [fraction]
    nifty_1d_pct: float | None = None
    nifty_1w_pct: float | None = None
    breadth_pct: float | None = None
    fii_net_cr: float | None = None
    vrp_median: float | None = None
    term_slope_median: float | None = None

    def as_prompt_block(self) -> str:
        """One-line summary for the Phase 3 LLM prompt."""
        lines = [f"REGIME={self.regime.upper()} (conf={self.confidence:.2f})"]
        lines.append(f"Preferred structures: {', '.join(self.strategy_playbook[:3])}")

        signals = []
        if self.vix_current:
            vel = f"{self.vix_velocity:+.1%}" if self.vix_velocity is not None else "n/a"
            signals.append(f"VIX={self.vix_current:.1f} (vel={vel})")
        if self.nifty_1d_pct is not None:
            signals.append(f"Nifty 1d={self.nifty_1d_pct:+.2f}% 1w={self.nifty_1w_pct:+.2f}%")
        if self.breadth_pct is not None:
            signals.append(f"breadth={self.breadth_pct:.0f}%")
        if self.vrp_median is not None:
            signals.append(f"VRP_median={self.vrp_median*100:+.1f}vpts")
        if signals:
            lines.append(" | ".join(signals))

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure classification logic (no I/O)
# ---------------------------------------------------------------------------

def classify_regime(
    vix_current: float | None,
    vix_prev_close: float | None,
    nifty_1d_pct: float | None,
    nifty_1w_pct: float | None,
    breadth_pct: float | None,
    fii_net_cr: float | None,
    vrp_median: float | None,
    *,
    vix_spike_threshold: float = 0.08,      # VIX up 8%+ in a day = expansion
    vix_drop_threshold: float = -0.07,      # VIX down 7%+ in a day = contraction
    vix_high_threshold: float = 18.0,       # VIX > 18 = elevated
    trend_daily_threshold: float = 1.5,     # |1d return| > 1.5% = trending
    trend_weekly_threshold: float = 2.5,    # |1w return| > 2.5% confirms trend
    breadth_bull_threshold: float = 65.0,   # breadth > 65% = bullish breadth
    breadth_bear_threshold: float = 35.0,   # breadth < 35% = bearish breadth
    vrp_cheap_threshold: float = 0.01,      # VRP < 0.01 = IV not rich
    vrp_rich_threshold: float = 0.02,       # VRP > 0.02 = IV rich
) -> tuple[str, float]:
    """Classify regime and return (regime_name, confidence_0_to_1).

    Priority order: vol_expansion → vol_contraction → trending_bear →
    trending_bull → range_high_iv → neutral.
    """
    vix_velocity: float | None = None
    if vix_current is not None and vix_prev_close is not None and vix_prev_close > 0:
        vix_velocity = (vix_current - vix_prev_close) / vix_prev_close

    # 1. vol_expansion: VIX spiking AND IV hasn't caught up with RV
    if (
        vix_velocity is not None and vix_velocity >= vix_spike_threshold
        and (vrp_median is None or vrp_median < vrp_cheap_threshold)
    ):
        conf = min(1.0, (vix_velocity / vix_spike_threshold) * 0.7
                   + (0.3 if vrp_median is not None and vrp_median < 0 else 0.15))
        return "vol_expansion", round(conf, 3)

    # 2. vol_contraction: VIX falling fast AND IV still elevated (rich vs RV)
    if (
        vix_velocity is not None and vix_velocity <= vix_drop_threshold
        and (vrp_median is not None and vrp_median >= vrp_rich_threshold)
    ):
        conf = min(1.0, (abs(vix_velocity) / abs(vix_drop_threshold)) * 0.6
                   + min(0.4, vrp_median * 10))
        return "vol_contraction", round(conf, 3)

    # 3. trending_bear: consistent multi-day downtrend with breadth collapse
    bear_conditions = [
        nifty_1d_pct is not None and nifty_1d_pct < -trend_daily_threshold,
        nifty_1w_pct is not None and nifty_1w_pct < -trend_weekly_threshold,
        breadth_pct is not None and breadth_pct < breadth_bear_threshold,
    ]
    if sum(bear_conditions) >= 2:
        conf = sum(bear_conditions) / 3.0
        # Boost confidence if FII is selling
        if fii_net_cr is not None and fii_net_cr < -2000:
            conf = min(1.0, conf + 0.15)
        return "trending_bear", round(conf, 3)

    # 4. trending_bull: consistent multi-day uptrend with broad participation
    bull_conditions = [
        nifty_1d_pct is not None and nifty_1d_pct > trend_daily_threshold,
        nifty_1w_pct is not None and nifty_1w_pct > trend_weekly_threshold,
        breadth_pct is not None and breadth_pct > breadth_bull_threshold,
    ]
    if sum(bull_conditions) >= 2:
        conf = sum(bull_conditions) / 3.0
        return "trending_bull", round(conf, 3)

    # 5. range_high_iv: VIX elevated but spot not trending strongly
    if vix_current is not None and vix_current >= vix_high_threshold:
        if nifty_1d_pct is None or abs(nifty_1d_pct) < trend_daily_threshold:
            conf = min(1.0, (vix_current - vix_high_threshold) / 5.0 * 0.6 + 0.3)
            return "range_high_iv", round(conf, 3)

    return "neutral", 0.5


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_latest_vix(session, before_date: date | None = None) -> tuple[float | None, float | None]:
    """Return (day_vix, prev_vix) from vix_ticks, optionally bounded to before_date.

    When before_date is provided (historical replay), only ticks with
    timestamp < (before_date + 1 day) are considered, giving the VIX
    reading as it was known on that date.
    """
    from datetime import timedelta
    from src.models.fno_vix import VIXTick
    q = select(VIXTick.vix_value, VIXTick.timestamp).order_by(VIXTick.timestamp.desc())
    if before_date is not None:
        cutoff = datetime(before_date.year, before_date.month, before_date.day,
                          23, 59, 59, tzinfo=timezone.utc)
        q = q.where(VIXTick.timestamp <= cutoff)
    rows = (await session.execute(q.limit(2))).all()
    if not rows:
        return None, None
    current = float(rows[0].vix_value)
    prev = float(rows[1].vix_value) if len(rows) > 1 else None
    return current, prev


async def _get_sentiment_inputs(session, before_date: date | None = None) -> dict:
    """Extract Nifty returns and breadth from sentiment row, bounded to before_date."""
    import json as _json
    from src.models.content import RawContent
    q = select(RawContent.content_text).where(RawContent.media_type == "sentiment")
    if before_date is not None:
        cutoff = datetime(before_date.year, before_date.month, before_date.day,
                          23, 59, 59, tzinfo=timezone.utc)
        q = q.where(RawContent.fetched_at <= cutoff)
    row = (await session.execute(q.order_by(RawContent.fetched_at.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return {}
    try:
        data = _json.loads(row)
        comps = data.get("components", {})
        return {
            "nifty_1d_pct": comps.get("trend_1d", {}).get("nifty_pct"),
            "nifty_1w_pct": comps.get("trend_1w", {}).get("nifty_pct"),
            "breadth_pct": comps.get("trend_1d", {}).get("breadth_pct"),
        }
    except Exception:
        return {}


async def _get_latest_fii(session, before_date: date | None = None) -> float | None:
    """Return latest FII net (crores) from raw_content, bounded to before_date."""
    import json as _json
    from src.models.content import RawContent
    q = select(RawContent.content_text).where(RawContent.media_type == "fii_dii")
    if before_date is not None:
        cutoff = datetime(before_date.year, before_date.month, before_date.day,
                          23, 59, 59, tzinfo=timezone.utc)
        q = q.where(RawContent.fetched_at <= cutoff)
    row = (await session.execute(q.order_by(RawContent.fetched_at.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return None
    try:
        data = _json.loads(row)
        return float(data.get("fii_net_cr") or 0)
    except Exception:
        return None


async def _get_median_vrp(session, run_date: date) -> float | None:
    """Compute median VRP across the F&O universe from iv_history."""
    from src.models.fno_iv import IVHistory
    rows = (await session.execute(
        select(IVHistory.vrp)
        .where(
            IVHistory.date == run_date,
            IVHistory.vrp.isnot(None),
            IVHistory.dryrun_run_id.is_(None),
        )
    )).scalars().all()
    if not rows:
        # Try yesterday
        rows = (await session.execute(
            select(IVHistory.vrp)
            .where(
                IVHistory.date == run_date - timedelta(days=1),
                IVHistory.vrp.isnot(None),
                IVHistory.dryrun_run_id.is_(None),
            )
        )).scalars().all()
    if not rows:
        return None
    vals = sorted(float(v) for v in rows)
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


async def _get_median_term_slope(session, run_date: date) -> float | None:
    """Compute median term slope from vol_surface_snapshot."""
    from src.models.fno_vol_surface import VolSurfaceSnapshot
    rows = (await session.execute(
        select(VolSurfaceSnapshot.term_slope)
        .where(
            VolSurfaceSnapshot.run_date == run_date,
            VolSurfaceSnapshot.term_slope.isnot(None),
        )
    )).scalars().all()
    if not rows:
        return None
    vals = sorted(float(v) for v in rows)
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


async def _upsert_regime(session, result: RegimeResult) -> None:
    # Use CAST(... AS jsonb) instead of ::jsonb — the :: cast after a named
    # parameter (:name::jsonb) confuses SQLAlchemy's parameter binding because
    # it parses the second colon as a new parameter marker.
    await session.execute(text("""
        INSERT INTO market_regime_snapshot
            (run_date, regime, confidence, strategy_playbook,
             vix_current, vix_prev_close, vix_velocity,
             nifty_1d_pct, nifty_1w_pct, breadth_pct,
             fii_net_cr, vrp_median, term_slope_median, features_json)
        VALUES
            (:run_date, :regime, :confidence, CAST(:playbook AS jsonb),
             :vix_current, :vix_prev, :vix_vel,
             :n1d, :n1w, :breadth,
             :fii, :vrp, :term_slope, CAST(:features AS jsonb))
        ON CONFLICT (run_date) DO UPDATE SET
            regime             = EXCLUDED.regime,
            confidence         = EXCLUDED.confidence,
            strategy_playbook  = EXCLUDED.strategy_playbook,
            vix_current        = EXCLUDED.vix_current,
            vix_prev_close     = EXCLUDED.vix_prev_close,
            vix_velocity       = EXCLUDED.vix_velocity,
            nifty_1d_pct       = EXCLUDED.nifty_1d_pct,
            nifty_1w_pct       = EXCLUDED.nifty_1w_pct,
            breadth_pct        = EXCLUDED.breadth_pct,
            fii_net_cr         = EXCLUDED.fii_net_cr,
            vrp_median         = EXCLUDED.vrp_median,
            term_slope_median  = EXCLUDED.term_slope_median,
            features_json      = EXCLUDED.features_json,
            computed_at        = NOW()
    """), {
        "run_date": result.run_date,
        "regime": result.regime,
        "confidence": result.confidence,
        "playbook": json.dumps(result.strategy_playbook),
        "vix_current": result.vix_current,
        "vix_prev": result.vix_prev_close,
        "vix_vel": result.vix_velocity,
        "n1d": result.nifty_1d_pct,
        "n1w": result.nifty_1w_pct,
        "breadth": result.breadth_pct,
        "fii": result.fii_net_cr,
        "vrp": result.vrp_median,
        "term_slope": result.term_slope_median,
        "features": json.dumps({
            "vix_current": result.vix_current,
            "vix_velocity": result.vix_velocity,
            "nifty_1d_pct": result.nifty_1d_pct,
            "nifty_1w_pct": result.nifty_1w_pct,
            "breadth_pct": result.breadth_pct,
            "fii_net_cr": result.fii_net_cr,
            "vrp_median": result.vrp_median,
        }),
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def compute_regime(run_date: date | None = None) -> RegimeResult:
    """Classify today's market regime and persist to market_regime_snapshot.

    Returns RegimeResult regardless of DB errors (always returns a usable regime).
    """
    if run_date is None:
        run_date = date.today()

    # For historical replay: bound all time-series reads to run_date so we
    # read data as it was known on that day, not today's latest values.
    hist_cutoff = run_date if run_date != date.today() else None

    async with session_scope() as session:
        vix_current, vix_prev = await _get_latest_vix(session, before_date=hist_cutoff)
        sentiment = await _get_sentiment_inputs(session, before_date=hist_cutoff)
        fii_net = await _get_latest_fii(session, before_date=hist_cutoff)
        vrp_median = await _get_median_vrp(session, run_date)
        term_median = await _get_median_term_slope(session, run_date)

    vix_vel = None
    if vix_current and vix_prev and vix_prev > 0:
        vix_vel = (vix_current - vix_prev) / vix_prev

    cfg = get_settings()
    regime, confidence = classify_regime(
        vix_current=vix_current,
        vix_prev_close=vix_prev,
        nifty_1d_pct=sentiment.get("nifty_1d_pct"),
        nifty_1w_pct=sentiment.get("nifty_1w_pct"),
        breadth_pct=sentiment.get("breadth_pct"),
        fii_net_cr=fii_net,
        vrp_median=vrp_median,
        vix_spike_threshold=cfg.fno_regime_vix_spike,
        vix_drop_threshold=cfg.fno_regime_vix_drop,
        vix_high_threshold=cfg.fno_vix_high_threshold,
        trend_daily_threshold=cfg.fno_regime_trend_1d,
        trend_weekly_threshold=cfg.fno_regime_trend_1w,
        breadth_bull_threshold=cfg.fno_regime_breadth_bull,
        breadth_bear_threshold=cfg.fno_regime_breadth_bear,
        vrp_cheap_threshold=cfg.fno_regime_vrp_cheap,
        vrp_rich_threshold=cfg.fno_regime_vrp_rich,
    )

    result = RegimeResult(
        run_date=run_date,
        regime=regime,
        confidence=confidence,
        strategy_playbook=STRATEGY_PLAYBOOKS[regime],
        vix_current=vix_current,
        vix_prev_close=vix_prev,
        vix_velocity=vix_vel,
        nifty_1d_pct=sentiment.get("nifty_1d_pct"),
        nifty_1w_pct=sentiment.get("nifty_1w_pct"),
        breadth_pct=sentiment.get("breadth_pct"),
        fii_net_cr=fii_net,
        vrp_median=vrp_median,
        term_slope_median=term_median,
    )

    try:
        async with session_scope() as session:
            await _upsert_regime(session, result)
    except Exception as exc:
        logger.warning(f"regime_classifier: DB write failed: {exc!r}")

    _vel_str = f" vel={vix_vel:+.1%}" if vix_vel is not None else ""
    logger.info(
        f"regime_classifier: {run_date} -> {regime.upper()} "
        f"(conf={confidence:.2f}) VIX={vix_current}{_vel_str} "
        f"Nifty1d={sentiment.get('nifty_1d_pct')}%"
    )
    return result


async def get_latest_regime(run_date: date | None = None) -> RegimeResult | None:
    """Return the most recent regime snapshot on or before run_date."""
    if run_date is None:
        run_date = date.today()
    async with session_scope() as session:
        row = (await session.execute(text("""
            SELECT regime, confidence, strategy_playbook,
                   vix_current, vix_prev_close, vix_velocity,
                   nifty_1d_pct, nifty_1w_pct, breadth_pct,
                   fii_net_cr, vrp_median, term_slope_median, run_date
            FROM market_regime_snapshot
            WHERE run_date <= :rd
            ORDER BY run_date DESC LIMIT 1
        """), {"rd": run_date})).first()
    if row is None:
        return None
    return RegimeResult(
        run_date=row.run_date,
        regime=row.regime,
        confidence=float(row.confidence) if row.confidence else 0.5,
        strategy_playbook=row.strategy_playbook or STRATEGY_PLAYBOOKS.get(row.regime, []),
        vix_current=float(row.vix_current) if row.vix_current else None,
        vix_prev_close=float(row.vix_prev_close) if row.vix_prev_close else None,
        vix_velocity=float(row.vix_velocity) if row.vix_velocity else None,
        nifty_1d_pct=float(row.nifty_1d_pct) if row.nifty_1d_pct else None,
        nifty_1w_pct=float(row.nifty_1w_pct) if row.nifty_1w_pct else None,
        breadth_pct=float(row.breadth_pct) if row.breadth_pct else None,
        fii_net_cr=float(row.fii_net_cr) if row.fii_net_cr else None,
        vrp_median=float(row.vrp_median) if row.vrp_median else None,
        term_slope_median=float(row.term_slope_median) if row.term_slope_median else None,
    )
