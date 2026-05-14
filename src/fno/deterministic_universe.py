"""Deterministic six-factor universe scorer — Phase 0.5 baseline.

Plan reference: docs/llm_feature_generator/implementation_plan.md §0.5.1–0.5.2.

This is the null hypothesis the LLM-feature pipeline has to beat. It builds a
parallel top-K universe per run_date and persists it to
``quant_universe_baseline`` for downstream Sharpe comparison.

Factors (each z-scored within run_date across the F&O universe):

  1. Liquidity            z(20d_ADV) + z(-mean_spread_bps) + z(OI_persistence_30d)
  2. IV-rank momentum     IV_rank_252d × ΔIV_rank_5d
  3. Realized-vol regime  decile(RV_20d) × sign(RV_20d - RV_60d)
  4. Trend strength       z((P - SMA_50)/ATR_20) × sign(OBV_slope)
  5. Mean-reversion       |RSI_14 - 50|/50 × (P - VWAP_20)/VWAP_20 × -1
  6. Microstructure       gap_z × ΔOI_pre_open × PCR_z

Composite v0 = equal-weighted sum of the six standardised sub-scores. Top-K
(default 25) by composite drops into ``quant_universe_baseline`` for the day.

The function family follows CLAUDE.md: every public entry accepts ``as_of``
and ``dryrun_run_id``.

Missing factors degrade gracefully. When an input is unavailable for an
instrument, the corresponding sub-score is NaN; we treat NaN as zero after
standardisation so the composite still ranks the instrument, just with less
information. The intent is to *start collecting* — Phase 5 will revisit
factor weighting via PCA on rolling IC (plan §0.5.1 v1).
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import numpy as np
from loguru import logger
from sqlalchemy import text

from src.db import session_scope


_TARGET_K = 25            # number of instruments persisted per run_date
_LOOKBACK_TRADING_DAYS = 60    # rolling window for z-score normalisation
_RV_REGIME_DECILE_BUCKETS = 10


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorRow:
    """Per-instrument scored row prior to persistence."""

    instrument_id: uuid.UUID
    symbol: str
    z_liquidity: float | None
    z_iv_rank_momentum: float | None
    z_rv_regime: float | None
    z_trend_strength: float | None
    z_mean_reversion: float | None
    z_microstructure: float | None
    composite: float


async def run_deterministic_baseline(
    run_date: date | None = None,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
    top_k: int = _TARGET_K,
) -> int:
    """Compute and persist the deterministic top-K for ``run_date``.

    Returns the number of rows persisted. The function is idempotent — a
    second call for the same (run_date, instrument_id, dryrun_run_id) is a
    no-op via the UNIQUE constraint and ON CONFLICT DO NOTHING.
    """
    if run_date is None:
        run_date = (as_of or datetime.now(tz=timezone.utc)).date()

    factors = await _compute_factors(run_date=run_date, dryrun_run_id=dryrun_run_id)
    if not factors:
        logger.info(f"deterministic_universe: no inputs for {run_date} — no rows written")
        return 0

    standardised = _standardise_and_score(factors)
    top = sorted(standardised, key=lambda r: r.composite, reverse=True)[:top_k]
    await _persist_top_k(top, run_date=run_date, dryrun_run_id=dryrun_run_id)
    logger.info(
        f"deterministic_universe: {len(top)} rows persisted for {run_date} "
        f"(top composite={top[0].composite:.3f}, bottom={top[-1].composite:.3f})"
    )
    return len(top)


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawInputs:
    """Per-instrument raw factor inputs read from DB."""

    instrument_id: uuid.UUID
    symbol: str

    # Price-daily derived (full history within lookback)
    closes: list[float]            # most-recent first
    volumes: list[int]
    highs: list[float]
    lows: list[float]
    opens: list[float]

    # IV history (most-recent first)
    iv_rank_series: list[float]    # 0-100 percentile, today's first
    rv_20d_series: list[float]     # annualised decimal
    rv_60d_value: float | None     # latest 60d realized vol (annualised)

    # Options chain derived (latest snapshot before run_date EOD)
    mean_spread_bps: float | None  # ATM avg spread, basis points
    oi_persistence_30d: float | None
    pcr_near_expiry: float | None
    pre_open_oi_change: float | None


async def _compute_factors(
    *, run_date: date, dryrun_run_id: uuid.UUID | None
) -> list[FactorRow]:
    """Compute raw factor values for every F&O underlying with sufficient data."""
    rows: list[FactorRow] = []
    inputs_by_id = await _load_inputs(run_date=run_date, dryrun_run_id=dryrun_run_id)

    for inp in inputs_by_id:
        row = _compute_one(inp)
        if row is not None:
            rows.append(row)

    return rows


async def _load_inputs(
    *, run_date: date, dryrun_run_id: uuid.UUID | None
) -> list[_RawInputs]:
    """Hydrate per-instrument inputs from price_daily + iv_history + options_chain.

    A separate query per source keeps each fast — F&O universe ≈ 200
    instruments × 60 days = 12k price rows, well within a single SELECT.
    """
    cutoff_lo = run_date - timedelta(days=int(_LOOKBACK_TRADING_DAYS * 1.6))  # weekends buffer

    async with session_scope() as session:
        # F&O underlyings — those with at least one fno_candidates row in the
        # past 30 days are the live universe. Falls back to every instrument
        # with iv_history rows if the candidate table is sparse.
        univ_sql = text("""
            SELECT DISTINCT i.id AS instrument_id, i.symbol
            FROM instruments i
            JOIN iv_history ih ON ih.instrument_id = i.id
            WHERE ih.date BETWEEN :lo AND :hi
        """)
        univ_rows = (await session.execute(
            univ_sql, {"lo": cutoff_lo, "hi": run_date}
        )).all()
        universe = {r.instrument_id: r.symbol for r in univ_rows}
        if not universe:
            return []

        # Price history (most-recent first within the window).
        price_sql = text("""
            SELECT instrument_id, date, close, volume, high, low, open
            FROM price_daily
            WHERE date BETWEEN :lo AND :hi
              AND instrument_id = ANY(:ids)
            ORDER BY instrument_id, date DESC
        """)
        price_rows = (await session.execute(
            price_sql,
            {"lo": cutoff_lo, "hi": run_date, "ids": list(universe.keys())},
        )).all()

        # IV history.
        iv_sql = text("""
            SELECT instrument_id, date, iv_rank_52w, rv_20d
            FROM iv_history
            WHERE date BETWEEN :lo AND :hi
              AND instrument_id = ANY(:ids)
            ORDER BY instrument_id, date DESC
        """)
        iv_rows = (await session.execute(
            iv_sql,
            {"lo": cutoff_lo, "hi": run_date, "ids": list(universe.keys())},
        )).all()

        # Latest vol surface snapshot per instrument (≤ run_date).
        surf_sql = text("""
            SELECT DISTINCT ON (instrument_id)
                   instrument_id, pcr_near_expiry
            FROM vol_surface_snapshot
            WHERE run_date <= :hi
              AND instrument_id = ANY(:ids)
            ORDER BY instrument_id, run_date DESC
        """)
        try:
            surf_rows = (await session.execute(
                surf_sql, {"hi": run_date, "ids": list(universe.keys())}
            )).all()
        except Exception as exc:
            logger.warning(f"deterministic_universe: vol_surface read skipped: {exc}")
            surf_rows = []

        # ATM bid-ask spread (basis points) — averaged across the latest
        # chain snapshots for the nearest expiry. Review fix P3 #12.
        spread_sql = text("""
            WITH latest AS (
                SELECT instrument_id, MAX(snapshot_at) AS snap_at
                FROM options_chain
                WHERE instrument_id = ANY(:ids)
                  AND snapshot_at <= :hi
                GROUP BY instrument_id
            ),
            nearest AS (
                SELECT oc.instrument_id, MIN(oc.expiry_date) AS expiry_date
                FROM options_chain oc
                JOIN latest l ON l.instrument_id = oc.instrument_id
                             AND l.snap_at = oc.snapshot_at
                WHERE oc.expiry_date >= :hi
                GROUP BY oc.instrument_id
            )
            SELECT oc.instrument_id,
                   AVG( CASE WHEN oc.bid_price > 0 AND oc.ask_price > 0
                             THEN (oc.ask_price - oc.bid_price)
                                  / NULLIF((oc.ask_price + oc.bid_price)/2.0, 0)
                             ELSE NULL END ) * 10000.0 AS mean_spread_bps
            FROM options_chain oc
            JOIN latest l   ON l.instrument_id = oc.instrument_id
                            AND l.snap_at = oc.snapshot_at
            JOIN nearest n  ON n.instrument_id = oc.instrument_id
                            AND n.expiry_date = oc.expiry_date
            GROUP BY oc.instrument_id
        """)
        try:
            spread_rows = (await session.execute(
                spread_sql, {"hi": run_date, "ids": list(universe.keys())}
            )).all()
        except Exception as exc:
            logger.warning(f"deterministic_universe: spread read skipped: {exc}")
            spread_rows = []

        # OI persistence — stdev/mean of ATM-strike OI across daily snapshots
        # over the lookback window. Lower CV = more persistent. We invert
        # so higher = better (matches the rest of the liquidity sub-score).
        oi_persistence_sql = text("""
            WITH atm_oi_daily AS (
                SELECT
                    oc.instrument_id,
                    DATE(oc.snapshot_at) AS d,
                    AVG(oc.oi)::FLOAT AS atm_oi
                FROM options_chain oc
                WHERE oc.instrument_id = ANY(:ids)
                  AND oc.snapshot_at >= :lo
                  AND oc.snapshot_at <= :hi
                  AND oc.option_type = 'CE'
                GROUP BY oc.instrument_id, DATE(oc.snapshot_at)
            )
            SELECT instrument_id,
                   AVG(atm_oi) AS oi_mean,
                   STDDEV(atm_oi) AS oi_std,
                   COUNT(*) AS n_days
            FROM atm_oi_daily
            GROUP BY instrument_id
            HAVING COUNT(*) >= 5
        """)
        try:
            oi_persistence_rows = (await session.execute(
                oi_persistence_sql, {
                    "lo": run_date - timedelta(days=45),
                    "hi": run_date,
                    "ids": list(universe.keys()),
                },
            )).all()
        except Exception as exc:
            logger.warning(f"deterministic_universe: OI persistence read skipped: {exc}")
            oi_persistence_rows = []

    # Group everything by instrument_id.
    prices_by_id: dict[uuid.UUID, list] = {}
    for r in price_rows:
        prices_by_id.setdefault(r.instrument_id, []).append(r)

    iv_by_id: dict[uuid.UUID, list] = {}
    for r in iv_rows:
        iv_by_id.setdefault(r.instrument_id, []).append(r)

    pcr_by_id = {r.instrument_id: float(r.pcr_near_expiry) if r.pcr_near_expiry else None
                 for r in surf_rows}
    spread_by_id = {r.instrument_id: float(r.mean_spread_bps) if r.mean_spread_bps else None
                    for r in spread_rows}
    oi_persistence_by_id: dict = {}
    for r in oi_persistence_rows:
        mean = float(r.oi_mean) if r.oi_mean else 0.0
        std = float(r.oi_std) if r.oi_std else 0.0
        if mean > 0:
            cv = std / mean
            # Invert so higher = more persistent (lower CV).
            oi_persistence_by_id[r.instrument_id] = 1.0 / (cv + 0.1)
        else:
            oi_persistence_by_id[r.instrument_id] = None

    inputs: list[_RawInputs] = []
    for inst_id, symbol in universe.items():
        p = prices_by_id.get(inst_id, [])
        i = iv_by_id.get(inst_id, [])

        # Require at least 20 daily bars and 20 IV rows for the trend/RV
        # factors to be computable. Under that, skip the instrument — it
        # would contribute too much noise to the standardisation.
        if len(p) < 20 or len(i) < 20:
            continue

        rv_20_series = [float(x.rv_20d) for x in i if x.rv_20d is not None]
        rv_60_value = _trailing_mean(rv_20_series, 60) if len(rv_20_series) >= 60 else None

        inputs.append(_RawInputs(
            instrument_id=inst_id,
            symbol=symbol,
            closes=[float(x.close) for x in p if x.close is not None],
            volumes=[int(x.volume or 0) for x in p],
            highs=[float(x.high) for x in p if x.high is not None],
            lows=[float(x.low) for x in p if x.low is not None],
            opens=[float(x.open) for x in p if x.open is not None],
            iv_rank_series=[float(x.iv_rank_52w) for x in i if x.iv_rank_52w is not None],
            rv_20d_series=rv_20_series,
            rv_60d_value=rv_60_value,
            mean_spread_bps=spread_by_id.get(inst_id),
            oi_persistence_30d=oi_persistence_by_id.get(inst_id),
            pcr_near_expiry=pcr_by_id.get(inst_id),
            pre_open_oi_change=None,         # requires intraday pre-open OI feed
        ))
    return inputs


# ---------------------------------------------------------------------------
# Factor math (pure, easy to unit test)
# ---------------------------------------------------------------------------


def _compute_one(inp: _RawInputs) -> FactorRow | None:
    """Compute the six raw factor values for one instrument.

    Returns None when *every* sub-score is NaN; otherwise returns a row with
    NaNs for the factors we couldn't compute. The standardisation step
    later replaces NaNs with 0 (neutral) so partial-data instruments still
    rank without dominating the composite.
    """
    closes = inp.closes
    volumes = inp.volumes

    # 1. Liquidity sub-score — z(20d_ADV) + z(-mean_spread_bps) + z(OI_persistence_30d).
    # Each component is summed at raw scale; the cross-sectional z-score
    # in _standardise_and_score puts the combined value on a comparable
    # scale across the universe (review fix P3 #12).
    adv20 = float(np.mean(volumes[:20])) if len(volumes) >= 20 else math.nan
    liq_components: list[float] = []
    if not math.isnan(adv20):
        liq_components.append(adv20 / 1e6)   # millions of shares — keeps magnitudes comparable
    if inp.mean_spread_bps is not None:
        # Negate so tighter spreads contribute positively.
        liq_components.append(-float(inp.mean_spread_bps) / 100.0)
    if inp.oi_persistence_30d is not None:
        liq_components.append(float(inp.oi_persistence_30d))
    liquidity = float(np.sum(liq_components)) if liq_components else math.nan

    # 2. IV-rank momentum — IV_rank × ΔIV_rank_5d, capped at sane bounds.
    iv_rank_today = inp.iv_rank_series[0] if inp.iv_rank_series else math.nan
    iv_rank_5d_ago = inp.iv_rank_series[5] if len(inp.iv_rank_series) > 5 else math.nan
    if math.isnan(iv_rank_today) or math.isnan(iv_rank_5d_ago):
        iv_rank_mom = math.nan
    else:
        delta = iv_rank_today - iv_rank_5d_ago
        iv_rank_mom = (iv_rank_today / 100.0) * delta

    # 3. Realised-vol regime — decile(RV_20d) × sign(RV_20d - RV_60d).
    rv_today = inp.rv_20d_series[0] if inp.rv_20d_series else math.nan
    if math.isnan(rv_today) or inp.rv_60d_value is None:
        rv_regime = math.nan
    else:
        decile = _decile(rv_today, inp.rv_20d_series[:60])
        rv_regime = decile * (1.0 if rv_today >= inp.rv_60d_value else -1.0)

    # 4. Trend strength — z((P - SMA50)/ATR20) × sign(OBV slope).
    if len(closes) >= 50 and len(inp.highs) >= 20 and len(inp.lows) >= 20:
        sma50 = float(np.mean(closes[:50]))
        atr20 = float(np.mean([h - l for h, l in zip(inp.highs[:20], inp.lows[:20])]))
        if atr20 > 0:
            trend_raw = (closes[0] - sma50) / atr20
        else:
            trend_raw = math.nan
        obv_sign = _obv_slope_sign(closes[:30], volumes[:30])
        trend = trend_raw * obv_sign if not math.isnan(trend_raw) else math.nan
    else:
        trend = math.nan

    # 5. Mean-reversion stretch — |RSI - 50|/50 × (P - VWAP20)/VWAP20 × -1.
    if len(closes) >= 20:
        rsi14 = _rsi(closes[:15])
        vwap20 = _vwap(closes[:20], volumes[:20])
        if rsi14 is not None and vwap20 is not None and vwap20 > 0:
            stretch = (closes[0] - vwap20) / vwap20
            mean_revert = (abs(rsi14 - 50.0) / 50.0) * stretch * -1.0
        else:
            mean_revert = math.nan
    else:
        mean_revert = math.nan

    # 6. Microstructure — gap_z × PCR_z. We don't yet have pre-open OI
    # deltas (requires intraday pre-open snapshots), so the spec's full
    # gap_z × ΔOI_pre_open × PCR_z degrades to a two-factor product.
    # Standardisation across the universe normalises the magnitude
    # (review fix P3 #12).
    microstructure = math.nan
    if len(inp.opens) >= 1 and len(closes) >= 2:
        # gap = (today open - prev close) / prev close
        try:
            today_open = inp.opens[0]
            prev_close = closes[1]
            gap = (today_open - prev_close) / prev_close if prev_close > 0 else 0.0
        except (IndexError, ZeroDivisionError):
            gap = 0.0
        pcr = inp.pcr_near_expiry
        if pcr is not None:
            # Centre PCR around 1.0 (neutral) so the sign carries direction.
            microstructure = float(gap * (pcr - 1.0))

    if all(math.isnan(v) for v in (liquidity, iv_rank_mom, rv_regime, trend, mean_revert, microstructure)):
        return None

    return FactorRow(
        instrument_id=inp.instrument_id,
        symbol=inp.symbol,
        z_liquidity=liquidity,           # standardised later
        z_iv_rank_momentum=iv_rank_mom,
        z_rv_regime=rv_regime,
        z_trend_strength=trend,
        z_mean_reversion=mean_revert,
        z_microstructure=microstructure,
        composite=0.0,                    # filled in by _standardise_and_score
    )


def _standardise_and_score(rows: list[FactorRow]) -> list[FactorRow]:
    """Cross-sectional z-score each factor, then equal-weighted composite.

    NaN → 0 after standardisation: keeps partial-data names in the ranking
    without letting them swing the composite. Single-instrument or
    zero-variance columns collapse to all-zero (no signal).
    """
    fields = (
        "z_liquidity",
        "z_iv_rank_momentum",
        "z_rv_regime",
        "z_trend_strength",
        "z_mean_reversion",
        "z_microstructure",
    )
    standardised_cols: dict[str, np.ndarray] = {}
    for field in fields:
        raw = np.array([getattr(r, field) for r in rows], dtype=float)
        with np.errstate(invalid="ignore"):
            mean = float(np.nanmean(raw)) if np.any(~np.isnan(raw)) else 0.0
            std = float(np.nanstd(raw)) if np.any(~np.isnan(raw)) else 0.0
        if std == 0.0 or not math.isfinite(std):
            standardised_cols[field] = np.zeros_like(raw)
        else:
            z = (raw - mean) / std
            z = np.nan_to_num(z, nan=0.0, posinf=0.0, neginf=0.0)
            standardised_cols[field] = z

    composite = np.sum(np.stack([standardised_cols[f] for f in fields]), axis=0)

    return [
        FactorRow(
            instrument_id=r.instrument_id,
            symbol=r.symbol,
            z_liquidity=float(standardised_cols["z_liquidity"][i]),
            z_iv_rank_momentum=float(standardised_cols["z_iv_rank_momentum"][i]),
            z_rv_regime=float(standardised_cols["z_rv_regime"][i]),
            z_trend_strength=float(standardised_cols["z_trend_strength"][i]),
            z_mean_reversion=float(standardised_cols["z_mean_reversion"][i]),
            z_microstructure=float(standardised_cols["z_microstructure"][i]),
            composite=float(composite[i]),
        )
        for i, r in enumerate(rows)
    ]


# ---------------------------------------------------------------------------
# Helper math
# ---------------------------------------------------------------------------


def _trailing_mean(series: list[float], n: int) -> float | None:
    if len(series) < n:
        return None
    return float(np.mean(series[:n]))


def _decile(value: float, distribution: list[float]) -> float:
    """Map ``value`` to its decile within ``distribution`` (0..10 scale)."""
    if not distribution:
        return 0.0
    sorted_d = np.sort(np.array(distribution, dtype=float))
    pct = float(np.searchsorted(sorted_d, value) / max(len(sorted_d), 1))
    return pct * _RV_REGIME_DECILE_BUCKETS


def _obv_slope_sign(closes: list[float], volumes: list[int]) -> float:
    """Sign of OBV linear slope over the window. Returns +1, -1, or 0."""
    if len(closes) < 5 or len(volumes) < 5:
        return 0.0
    # Reverse so OBV builds chronologically (oldest first).
    closes = closes[::-1]
    volumes = volumes[::-1]
    obv = [0]
    for k in range(1, len(closes)):
        direction = 1 if closes[k] > closes[k - 1] else (-1 if closes[k] < closes[k - 1] else 0)
        obv.append(obv[-1] + direction * volumes[k])
    obv_arr = np.array(obv, dtype=float)
    x = np.arange(len(obv_arr), dtype=float)
    slope = float(np.polyfit(x, obv_arr, 1)[0]) if len(obv_arr) >= 2 else 0.0
    if slope > 0:
        return 1.0
    if slope < 0:
        return -1.0
    return 0.0


def _rsi(closes_recent_first: list[float]) -> float | None:
    """Wilder's RSI on a 14-period window. ``closes_recent_first[0]`` is
    today; returns the RSI value 0–100, or None if there's not enough data."""
    if len(closes_recent_first) < 15:
        return None
    closes = closes_recent_first[::-1]   # chronological
    deltas = np.diff(closes[-15:])
    gains = np.clip(deltas, 0, None)
    losses = -np.clip(deltas, None, 0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _vwap(closes_recent_first: list[float], volumes_recent_first: list[int]) -> float | None:
    """Volume-weighted average price over the supplied window."""
    if not closes_recent_first or not volumes_recent_first:
        return None
    closes = np.array(closes_recent_first, dtype=float)
    vols = np.array(volumes_recent_first, dtype=float)
    total_vol = float(np.sum(vols))
    if total_vol <= 0:
        return float(np.mean(closes))
    return float(np.sum(closes * vols) / total_vol)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _persist_top_k(
    rows: Iterable[FactorRow], *, run_date: date, dryrun_run_id: uuid.UUID | None
) -> None:
    """Write top-K rows to quant_universe_baseline (ON CONFLICT DO NOTHING)."""
    payload = [
        {
            "run_date": run_date,
            "instrument_id": str(r.instrument_id),
            "rank": idx + 1,
            "composite_score": r.composite,
            "z_liquidity": r.z_liquidity,
            "z_iv_rank_momentum": r.z_iv_rank_momentum,
            "z_rv_regime": r.z_rv_regime,
            "z_trend_strength": r.z_trend_strength,
            "z_mean_reversion": r.z_mean_reversion,
            "z_microstructure": r.z_microstructure,
            "dryrun_run_id": str(dryrun_run_id) if dryrun_run_id is not None else None,
        }
        for idx, r in enumerate(rows)
    ]
    if not payload:
        return

    insert_sql = text("""
        INSERT INTO quant_universe_baseline (
            run_date, instrument_id, rank, composite_score,
            z_liquidity, z_iv_rank_momentum, z_rv_regime,
            z_trend_strength, z_mean_reversion, z_microstructure,
            dryrun_run_id
        ) VALUES (
            :run_date, :instrument_id, :rank, :composite_score,
            :z_liquidity, :z_iv_rank_momentum, :z_rv_regime,
            :z_trend_strength, :z_mean_reversion, :z_microstructure,
            :dryrun_run_id
        )
        ON CONFLICT (run_date, instrument_id, dryrun_run_id) DO NOTHING
    """)
    async with session_scope() as session:
        await session.execute(insert_sql, payload)
