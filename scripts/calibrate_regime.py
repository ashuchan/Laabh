#!/usr/bin/env python3
"""Calibrate regime classifier thresholds from historical data.

Usage:
    python scripts/calibrate_regime.py              # dry-run (print only)
    python scripts/calibrate_regime.py --update-env # write calibrated values to .env
    python scripts/calibrate_regime.py --backfill-vrp --update-env

Thresholds calibrated (quantile-based, in classifier waterfall order):
    FNO_REGIME_VIX_SPIKE      P90 of up-day VIX velocities
    FNO_REGIME_VIX_DROP       P10 of down-day VIX velocities
    FNO_VIX_HIGH_THRESHOLD    P65 of VIX level (shared key; also used by vix_collector)
    FNO_REGIME_TREND_1D       P80 of |Nifty 1d return|
    FNO_REGIME_TREND_1W       P80 of |Nifty 1w return|
    FNO_REGIME_BREADTH_BULL   P80 of breadth distribution  (needs >= 60 trading days)
    FNO_REGIME_BREADTH_BEAR   P20 of breadth distribution  (needs >= 60 trading days)
    FNO_REGIME_VRP_CHEAP      P25 of daily VRP-median      (needs >= 30 trading days)
    FNO_REGIME_VRP_RICH       P75 of daily VRP-median      (needs >= 30 trading days)

Design choices:
  - market_regime_snapshot is read first; raw tables supplement earlier dates.
    This ensures feature values match what the live classifier used (sentiment
    collector output for breadth/nifty, not raw price_daily returns).
  - GMM clustering is deliberately omitted: the dataset is typically < 90 days,
    which makes a 6-component, 6-feature GMM heavily underdetermined.
  - FII threshold (fii_net_cr < -2000 sentinel) is left unchanged; data is
    too sparse (< 15 rows) for statistical calibration.
  - VRP thresholds here are distinct from FNO_VRP_CHEAP_THRESHOLD / FNO_VRP_RICH_THRESHOLD
    (the latter label individual instruments in vrp_engine.py). These label the
    cross-sectional median VRP across the whole F&O universe.

Re-run every ~3 months once breadth data reaches MIN_BREADTH_DAYS=60 trading days.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from sqlalchemy import text

from src.config import get_settings
from src.db import session_scope
from src.fno.regime_classifier import STRATEGY_PLAYBOOKS, classify_regime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_BREADTH_DAYS = 60    # below this: breadth thresholds fall back to defaults
MIN_VRP_DAYS = 30        # below this: VRP thresholds fall back to defaults
MIN_CALIB_DAYS = 20      # need at least this many total days for any calibration

TARGET_REGIME_FREQ: dict[str, tuple[float, float]] = {
    "vol_expansion":   (0.05, 0.10),
    "vol_contraction": (0.08, 0.12),
    "trending_bear":   (0.15, 0.20),
    "trending_bull":   (0.15, 0.20),
    "range_high_iv":   (0.20, 0.25),
    "neutral":         (0.25, 0.35),
}

# Mirrors current hardcoded defaults in classify_regime() kwargs
DEFAULTS: dict[str, float] = {
    "FNO_REGIME_VIX_SPIKE":    0.08,
    "FNO_REGIME_VIX_DROP":    -0.07,
    "FNO_VIX_HIGH_THRESHOLD": 18.0,
    "FNO_REGIME_TREND_1D":     1.5,
    "FNO_REGIME_TREND_1W":     2.5,
    "FNO_REGIME_BREADTH_BULL": 65.0,
    "FNO_REGIME_BREADTH_BEAR": 35.0,
    "FNO_REGIME_VRP_CHEAP":    0.01,
    "FNO_REGIME_VRP_RICH":     0.02,
}


# ---------------------------------------------------------------------------
# Data availability check
# ---------------------------------------------------------------------------

async def check_data_availability() -> dict:
    """Query the DB for a summary of available historical data."""
    async with session_scope() as session:
        row = (await session.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM vix_ticks)
                                                                    AS vix_rows,
                (SELECT MIN(DATE(timestamp)) FROM vix_ticks)        AS vix_from,
                (SELECT MAX(DATE(timestamp)) FROM vix_ticks)        AS vix_to,
                (SELECT COUNT(*) FROM iv_history
                 WHERE vrp IS NOT NULL AND dryrun_run_id IS NULL)   AS vrp_rows,
                (SELECT COUNT(DISTINCT date) FROM iv_history
                 WHERE vrp IS NOT NULL AND dryrun_run_id IS NULL)   AS vrp_days,
                (SELECT COUNT(DISTINCT date) FROM iv_history
                 WHERE atm_iv > 0 AND vrp IS NULL
                   AND dryrun_run_id IS NULL)                       AS vrp_backfill_needed,
                (SELECT COUNT(*) FROM price_daily
                 WHERE instrument_id = (
                   SELECT id FROM instruments
                   WHERE symbol = 'NIFTY 50' LIMIT 1))             AS nifty_rows,
                (SELECT MIN(date) FROM price_daily
                 WHERE instrument_id = (
                   SELECT id FROM instruments
                   WHERE symbol = 'NIFTY 50' LIMIT 1))             AS nifty_from,
                (SELECT COUNT(DISTINCT DATE(fetched_at))
                 FROM raw_content
                 WHERE media_type = 'sentiment')                    AS breadth_days,
                (SELECT COUNT(*) FROM fno_signals
                 WHERE final_pnl IS NOT NULL
                   AND dryrun_run_id IS NULL)                       AS closed_signals,
                (SELECT COUNT(*) FROM market_regime_snapshot)       AS snapshot_rows
        """))).first()
    return {k: v for k, v in row._mapping.items()}


# ---------------------------------------------------------------------------
# VRP backfill
# ---------------------------------------------------------------------------

async def run_vrp_backfill() -> int:
    """Backfill VRP for all dates that have atm_iv but no VRP data."""
    from src.fno.vrp_engine import compute_vrp_for_date_range

    async with session_scope() as session:
        row = (await session.execute(text(
            "SELECT MIN(date) mn, MAX(date) mx FROM iv_history "
            "WHERE atm_iv > 0 AND dryrun_run_id IS NULL"
        ))).first()

    if not (row and row.mn):
        print("No iv_history rows with atm_iv found; skipping VRP backfill.")
        return 0

    print(f"Running VRP backfill from {row.mn} to {row.mx} (may take several minutes)...")
    n = await compute_vrp_for_date_range(row.mn, row.mx)
    print(f"VRP backfill complete: {n} rows updated.")
    return n


# ---------------------------------------------------------------------------
# Feature dataset builder
# ---------------------------------------------------------------------------

async def build_feature_dataset() -> pd.DataFrame:
    """Build daily feature matrix for threshold calibration.

    Columns: date, vix_current, vix_prev_close, nifty_1d_pct, nifty_1w_pct,
             breadth_pct, vrp_median

    Reads market_regime_snapshot first (features are consistent with the live
    classifier's actual inputs). Supplements with raw-table extraction for
    earlier dates not in the snapshot.
    """
    # --- 1. market_regime_snapshot (authoritative for deployed-system dates) ---
    async with session_scope() as session:
        snap_rows = (await session.execute(text("""
            SELECT
                run_date       AS date,
                vix_current,
                vix_prev_close,
                nifty_1d_pct,
                nifty_1w_pct,
                breadth_pct,
                vrp_median
            FROM market_regime_snapshot
            ORDER BY run_date
        """))).all()

    snap_df = (
        pd.DataFrame([dict(r._mapping) for r in snap_rows])
        if snap_rows
        else pd.DataFrame(columns=[
            "date", "vix_current", "vix_prev_close",
            "nifty_1d_pct", "nifty_1w_pct", "breadth_pct", "vrp_median",
        ])
    )
    snap_dates: set = set(snap_df["date"].tolist()) if not snap_df.empty else set()

    # --- 2. Raw-table supplement for pre-snapshot dates ---

    # Daily closing VIX (last tick per UTC date)
    async with session_scope() as session:
        vix_rows = (await session.execute(text("""
            SELECT
                DATE(timestamp AT TIME ZONE 'UTC') AS date,
                (array_agg(vix_value ORDER BY timestamp DESC))[1] AS vix_close
            FROM vix_ticks
            GROUP BY DATE(timestamp AT TIME ZONE 'UTC')
            ORDER BY date
        """))).all()

    vix_df = pd.DataFrame(
        [{"date": r.date, "vix_current": float(r.vix_close)} for r in vix_rows]
    ).sort_values("date").reset_index(drop=True)
    if not vix_df.empty:
        vix_df["vix_prev_close"] = vix_df["vix_current"].shift(1)

    # Daily Nifty closes → 1d and 1w returns
    async with session_scope() as session:
        nifty_rows = (await session.execute(text("""
            SELECT p.date, p.close
            FROM price_daily p
            JOIN instruments i ON p.instrument_id = i.id
            WHERE i.symbol = 'NIFTY 50'
            ORDER BY p.date
        """))).all()

    nifty_df = pd.DataFrame(
        [{"date": r.date, "close": float(r.close)} for r in nifty_rows]
    ).sort_values("date").reset_index(drop=True)
    if not nifty_df.empty:
        nifty_df["nifty_1d_pct"] = nifty_df["close"].pct_change() * 100
        nifty_df["nifty_1w_pct"] = nifty_df["close"].pct_change(periods=5) * 100
        nifty_df = nifty_df.drop(columns=["close"])

    # Daily breadth from sentiment collector output (JSON in raw_content)
    async with session_scope() as session:
        breadth_rows = (await session.execute(text("""
            SELECT
                DATE(fetched_at AT TIME ZONE 'UTC') AS date,
                content_text
            FROM raw_content
            WHERE media_type = 'sentiment'
            ORDER BY fetched_at
        """))).all()

    breadth_records: list[dict] = []
    for r in breadth_rows:
        try:
            data = json.loads(r.content_text)
            bpct = data.get("components", {}).get("trend_1d", {}).get("breadth_pct")
            if bpct is not None:
                breadth_records.append({"date": r.date, "breadth_pct": float(bpct)})
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    breadth_df = (
        pd.DataFrame(breadth_records)
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
        if breadth_records
        else pd.DataFrame(columns=["date", "breadth_pct"])
    )

    # Daily VRP median across F&O universe
    async with session_scope() as session:
        vrp_rows = (await session.execute(text("""
            SELECT
                date,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY vrp) AS vrp_median
            FROM iv_history
            WHERE vrp IS NOT NULL AND dryrun_run_id IS NULL
            GROUP BY date
            ORDER BY date
        """))).all()

    vrp_df = (
        pd.DataFrame([{"date": r.date, "vrp_median": float(r.vrp_median)} for r in vrp_rows])
        if vrp_rows
        else pd.DataFrame(columns=["date", "vrp_median"])
    )

    # Merge raw tables into supplement frame
    if vix_df.empty:
        supp_df = pd.DataFrame(columns=[
            "date", "vix_current", "vix_prev_close",
            "nifty_1d_pct", "nifty_1w_pct", "breadth_pct", "vrp_median",
        ])
    else:
        supp_df = vix_df.copy()

        if not nifty_df.empty:
            supp_df = supp_df.merge(nifty_df, on="date", how="left")
        else:
            supp_df["nifty_1d_pct"] = np.nan
            supp_df["nifty_1w_pct"] = np.nan

        if not breadth_df.empty:
            supp_df = supp_df.merge(breadth_df, on="date", how="left")
        else:
            supp_df["breadth_pct"] = np.nan

        if not vrp_df.empty:
            supp_df = supp_df.merge(vrp_df, on="date", how="left")
        else:
            supp_df["vrp_median"] = np.nan

    # Exclude dates already covered by the snapshot
    if snap_dates and not supp_df.empty:
        supp_df = supp_df[~supp_df["date"].isin(snap_dates)]

    # Combine snapshot + supplement
    combined = pd.concat(
        [df for df in [snap_df, supp_df] if not df.empty],
        ignore_index=True,
    )
    if combined.empty:
        return combined

    combined["date"] = pd.to_datetime(combined["date"]).dt.date
    combined = (
        combined
        .sort_values("date")
        .drop_duplicates(subset=["date"])
        .reset_index(drop=True)
    )
    return combined


# ---------------------------------------------------------------------------
# Threshold computation
# ---------------------------------------------------------------------------

def _pct(series: pd.Series, q: float) -> float:
    return float(np.percentile(series.dropna().values, q))


def compute_thresholds(
    df: pd.DataFrame,
) -> tuple[dict[str, float], dict[str, str]]:
    """Compute calibrated thresholds via empirical quantile method.

    Calibration respects the classifier's waterfall priority order:
    thresholds for higher-priority regimes are computed first. Quantiles are
    derived from the raw feature distributions (not residual pools), which is
    a valid approximation when regime frequencies are relatively small.

    Returns (thresholds, notes). notes[key] explains each value's source.
    """
    thresholds = DEFAULTS.copy()
    notes: dict[str, str] = {}

    n = len(df)
    if n < MIN_CALIB_DAYS:
        for k in thresholds:
            notes[k] = f"default — only {n} days total (need >= {MIN_CALIB_DAYS})"
        return thresholds, notes

    # Derive VIX velocity for calibration (not stored in snap_df directly)
    vix_vel = pd.Series(dtype=float)
    if {"vix_current", "vix_prev_close"}.issubset(df.columns):
        mask = df["vix_prev_close"].notna() & (df["vix_prev_close"] > 0)
        vix_vel = (
            (df.loc[mask, "vix_current"] - df.loc[mask, "vix_prev_close"])
            / df.loc[mask, "vix_prev_close"]
        )

    # --- Priority 1: vol_expansion — VIX spike threshold ---
    up_vel = vix_vel[vix_vel > 0].dropna()
    if len(up_vel) >= 10:
        thresholds["FNO_REGIME_VIX_SPIKE"] = round(_pct(up_vel, 90), 4)
        notes["FNO_REGIME_VIX_SPIKE"] = f"P90 of up-day VIX velocities (n={len(up_vel)})"
    else:
        notes["FNO_REGIME_VIX_SPIKE"] = f"default — only {len(up_vel)} up-day obs (need >= 10)"

    # --- Priority 2: vol_contraction — VIX drop threshold (stays negative) ---
    dn_vel = vix_vel[vix_vel < 0].dropna()
    if len(dn_vel) >= 10:
        thresholds["FNO_REGIME_VIX_DROP"] = round(_pct(dn_vel, 10), 4)
        notes["FNO_REGIME_VIX_DROP"] = f"P10 of down-day VIX velocities (n={len(dn_vel)})"
    else:
        notes["FNO_REGIME_VIX_DROP"] = f"default — only {len(dn_vel)} down-day obs (need >= 10)"

    # --- range_high_iv + trend context: VIX high threshold (P65 of VIX level) ---
    vix_vals = df["vix_current"].dropna()
    if len(vix_vals) >= MIN_CALIB_DAYS:
        thresholds["FNO_VIX_HIGH_THRESHOLD"] = round(_pct(vix_vals, 65), 2)
        notes["FNO_VIX_HIGH_THRESHOLD"] = f"P65 of VIX level (n={len(vix_vals)})"
    else:
        notes["FNO_VIX_HIGH_THRESHOLD"] = f"default — only {len(vix_vals)} VIX observations"

    # --- Priority 3/4: trending_bear/bull — trend thresholds ---
    n1d = df["nifty_1d_pct"].dropna()
    if len(n1d) >= MIN_CALIB_DAYS:
        thresholds["FNO_REGIME_TREND_1D"] = round(_pct(n1d.abs(), 80), 2)
        notes["FNO_REGIME_TREND_1D"] = f"P80 of |Nifty 1d return| (n={len(n1d)})"
    else:
        notes["FNO_REGIME_TREND_1D"] = f"default — only {len(n1d)} 1d return obs"

    n1w = df["nifty_1w_pct"].dropna()
    if len(n1w) >= MIN_CALIB_DAYS:
        thresholds["FNO_REGIME_TREND_1W"] = round(_pct(n1w.abs(), 80), 2)
        notes["FNO_REGIME_TREND_1W"] = f"P80 of |Nifty 1w return| (n={len(n1w)})"
    else:
        notes["FNO_REGIME_TREND_1W"] = f"default — only {len(n1w)} 1w return obs"

    # --- Breadth thresholds (P80 / P20) — requires MIN_BREADTH_DAYS ---
    breadth = df["breadth_pct"].dropna()
    if len(breadth) >= MIN_BREADTH_DAYS:
        thresholds["FNO_REGIME_BREADTH_BULL"] = round(_pct(breadth, 80), 1)
        thresholds["FNO_REGIME_BREADTH_BEAR"] = round(_pct(breadth, 20), 1)
        notes["FNO_REGIME_BREADTH_BULL"] = f"P80 of breadth distribution (n={len(breadth)})"
        notes["FNO_REGIME_BREADTH_BEAR"] = f"P20 of breadth distribution (n={len(breadth)})"
    else:
        msg = f"default — only {len(breadth)} breadth days (need >= {MIN_BREADTH_DAYS})"
        notes["FNO_REGIME_BREADTH_BULL"] = msg
        notes["FNO_REGIME_BREADTH_BEAR"] = msg

    # --- VRP thresholds (P25 / P75) — requires MIN_VRP_DAYS ---
    vrp_vals = df["vrp_median"].dropna()
    if len(vrp_vals) >= MIN_VRP_DAYS:
        thresholds["FNO_REGIME_VRP_CHEAP"] = round(_pct(vrp_vals, 25), 4)
        thresholds["FNO_REGIME_VRP_RICH"] = round(_pct(vrp_vals, 75), 4)
        notes["FNO_REGIME_VRP_CHEAP"] = f"P25 of daily VRP-median (n={len(vrp_vals)})"
        notes["FNO_REGIME_VRP_RICH"] = f"P75 of daily VRP-median (n={len(vrp_vals)})"
    else:
        msg = f"default — only {len(vrp_vals)} VRP days (need >= {MIN_VRP_DAYS})"
        notes["FNO_REGIME_VRP_CHEAP"] = msg
        notes["FNO_REGIME_VRP_RICH"] = msg

    return thresholds, notes


# ---------------------------------------------------------------------------
# Regime frequency simulation
# ---------------------------------------------------------------------------

def _thresholds_to_kwargs(t: dict[str, float]) -> dict:
    return {
        "vix_spike_threshold":    t["FNO_REGIME_VIX_SPIKE"],
        "vix_drop_threshold":     t["FNO_REGIME_VIX_DROP"],
        "vix_high_threshold":     t["FNO_VIX_HIGH_THRESHOLD"],
        "trend_daily_threshold":  t["FNO_REGIME_TREND_1D"],
        "trend_weekly_threshold": t["FNO_REGIME_TREND_1W"],
        "breadth_bull_threshold": t["FNO_REGIME_BREADTH_BULL"],
        "breadth_bear_threshold": t["FNO_REGIME_BREADTH_BEAR"],
        "vrp_cheap_threshold":    t["FNO_REGIME_VRP_CHEAP"],
        "vrp_rich_threshold":     t["FNO_REGIME_VRP_RICH"],
    }


def _feat(row: pd.Series, col: str) -> float | None:
    if col not in row.index:
        return None
    v = row[col]
    return None if pd.isna(v) else float(v)


def simulate_regime_frequencies(
    df: pd.DataFrame,
    thresholds: dict[str, float],
) -> dict[str, int]:
    """Classify every row in df with the given thresholds; return per-regime counts."""
    counts: dict[str, int] = {r: 0 for r in STRATEGY_PLAYBOOKS}
    kwargs = _thresholds_to_kwargs(thresholds)

    for _, row in df.iterrows():
        regime, _ = classify_regime(
            vix_current=_feat(row, "vix_current"),
            vix_prev_close=_feat(row, "vix_prev_close"),
            nifty_1d_pct=_feat(row, "nifty_1d_pct"),
            nifty_1w_pct=_feat(row, "nifty_1w_pct"),
            breadth_pct=_feat(row, "breadth_pct"),
            fii_net_cr=None,
            vrp_median=_feat(row, "vrp_median"),
            **kwargs,
        )
        counts[regime] = counts.get(regime, 0) + 1

    return counts


# ---------------------------------------------------------------------------
# Alignment score on closed F&O signals
# ---------------------------------------------------------------------------

async def compute_alignment_score(
    df: pd.DataFrame,
    thresholds: dict[str, float],
) -> dict:
    """Check whether closed signals' strategy types matched the prevailing regime.

    n is typically very small (~27). Wilson CI at n=27, p=0.5 is ~+/-20%.
    Treat result as directional only, not statistically conclusive.
    """
    async with session_scope() as session:
        sig_rows = (await session.execute(text("""
            SELECT
                strategy_type,
                final_pnl,
                DATE(COALESCE(filled_at, proposed_at) AT TIME ZONE 'UTC') AS entry_date
            FROM fno_signals
            WHERE final_pnl IS NOT NULL
              AND dryrun_run_id IS NULL
            ORDER BY entry_date
        """))).all()

    if not sig_rows:
        return {"total": 0, "with_regime": 0, "aligned": 0,
                "aligned_profitable": 0, "score": None}

    df_indexed = df.set_index("date") if not df.empty and "date" in df.columns else pd.DataFrame()
    kwargs = _thresholds_to_kwargs(thresholds)

    total = len(sig_rows)
    with_regime = 0
    aligned = 0
    aligned_profitable = 0

    for sig in sig_rows:
        entry_date = sig.entry_date
        if df_indexed.empty or entry_date not in df_indexed.index:
            continue
        feat = df_indexed.loc[entry_date]
        # loc may return a DataFrame if multiple rows exist for the same date
        if isinstance(feat, pd.DataFrame):
            feat = feat.iloc[0]
        with_regime += 1

        regime, _ = classify_regime(
            vix_current=_feat(feat, "vix_current"),
            vix_prev_close=_feat(feat, "vix_prev_close"),
            nifty_1d_pct=_feat(feat, "nifty_1d_pct"),
            nifty_1w_pct=_feat(feat, "nifty_1w_pct"),
            breadth_pct=_feat(feat, "breadth_pct"),
            fii_net_cr=None,
            vrp_median=_feat(feat, "vrp_median"),
            **kwargs,
        )
        is_aligned = sig.strategy_type in STRATEGY_PLAYBOOKS.get(regime, [])
        if is_aligned:
            aligned += 1
            if sig.final_pnl is not None and sig.final_pnl > 0:
                aligned_profitable += 1

    return {
        "total": total,
        "with_regime": with_regime,
        "aligned": aligned,
        "aligned_profitable": aligned_profitable,
        "score": aligned_profitable / total if total > 0 else None,
    }


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(
    avail: dict,
    df: pd.DataFrame,
    old_thresholds: dict,
    new_thresholds: dict,
    notes: dict,
    old_freq: dict,
    new_freq: dict,
    alignment: dict,
) -> None:
    W = 72
    print("\n" + "=" * W)
    print("  REGIME CLASSIFIER CALIBRATION REPORT")
    print(f"  Run date: {date.today()}")
    print("=" * W)

    # --- Data availability ---
    print("\n-- Data Availability " + "-" * 51)
    print(f"  VIX ticks:              {avail['vix_rows']:>7,}  ({avail['vix_from']} to {avail['vix_to']})")
    print(f"  Nifty daily closes:     {avail['nifty_rows']:>7,}  (from {avail['nifty_from']})")
    print(f"  Breadth days:           {avail['breadth_days']:>7}")
    print(f"  VRP days (post-fill):   {avail['vrp_days']:>7}")
    print(f"  VRP backfill needed:    {avail['vrp_backfill_needed']:>7}  (run --backfill-vrp if > 0)")
    print(f"  Closed F&O signals:     {avail['closed_signals']:>7}")
    print(f"  Regime snapshot rows:   {avail['snapshot_rows']:>7}")
    print(f"  Feature matrix total:   {len(df):>7}  trading days")

    if len(df) < MIN_CALIB_DAYS:
        print(f"\n  WARNING: < {MIN_CALIB_DAYS} days of data — all thresholds remain at defaults.")

    breadth_n = int(df["breadth_pct"].notna().sum()) if "breadth_pct" in df.columns else 0
    if breadth_n < MIN_BREADTH_DAYS:
        deficit = MIN_BREADTH_DAYS - breadth_n
        print(
            f"\n  NOTE: Breadth data is {deficit} trading days short of the {MIN_BREADTH_DAYS}-day "
            "minimum. Breadth thresholds fall back to defaults until the sentiment "
            "collector has been running long enough."
        )

    vrp_n = int(df["vrp_median"].notna().sum()) if "vrp_median" in df.columns else 0
    if vrp_n < MIN_VRP_DAYS:
        deficit = MIN_VRP_DAYS - vrp_n
        print(
            f"\n  NOTE: VRP data is {deficit} days short of the {MIN_VRP_DAYS}-day minimum. "
            "Run --backfill-vrp to populate historical VRP, then re-run this script."
        )

    # --- Threshold table ---
    print("\n-- Threshold Calibration " + "-" * 47)
    print(f"  {'ENV KEY':<30} {'OLD':>8} {'NEW':>8}  NOTE")
    print(f"  {'-'*30} {'-'*8} {'-'*8}  {'-'*28}")
    for key in DEFAULTS:
        old = old_thresholds[key]
        new = new_thresholds[key]
        changed = "<--" if abs(old - new) > 1e-9 else "   "
        note = notes.get(key, "")
        print(f"  {key:<30} {old:>8.4f} {new:>8.4f} {changed} {note}")

    # --- Regime frequency table ---
    n_total = max(len(df), 1)
    print("\n-- Regime Frequency Simulation " + "-" * 40)
    print(f"  {'REGIME':<20} {'OLD':>6} {'NEW':>6}  {'TARGET RANGE'}")
    print(f"  {'-'*20} {'-'*6} {'-'*6}  {'-'*20}")
    for regime, (lo, hi) in TARGET_REGIME_FREQ.items():
        old_pct = old_freq.get(regime, 0) / n_total
        new_pct = new_freq.get(regime, 0) / n_total
        status = "[OK]" if lo <= new_pct <= hi else "[--]"
        print(
            f"  {regime:<20} {old_pct:>5.1%} {new_pct:>5.1%}  "
            f"{status} [{lo:.0%}-{hi:.0%}]"
        )

    # --- Alignment score ---
    al = alignment
    print("\n-- Alignment Score on Closed Trades " + "-" * 35)
    print(f"  Closed signals total:   {al['total']}")
    print(f"  With regime data:       {al['with_regime']}")
    print(f"  Strategy aligned:       {al['aligned']}")
    print(f"  Aligned + profitable:   {al['aligned_profitable']}")
    if al["score"] is not None:
        n = al["total"]
        p = al["score"]
        ci_half = 1.96 * (p * (1 - p) / max(n, 1)) ** 0.5
        print(f"  Score (aligned+pnl>0 / total): {p:.1%}")
        print(
            f"  NOTE: n={n} is very small. Wilson CI (95%) ~ +/-{ci_half:.1%}. "
            "Directional indicator only."
        )
    else:
        print("  No closed signal data available.")

    # --- Semantic note on VRP thresholds ---
    print("\n-- VRP Threshold Semantics " + "-" * 45)
    print("  FNO_REGIME_VRP_CHEAP/RICH classify the cross-sectional MEDIAN VRP")
    print("  across the F&O universe. They are distinct from FNO_VRP_CHEAP_THRESHOLD")
    print("  / FNO_VRP_RICH_THRESHOLD (vrp_engine.py) which label individual")
    print("  instruments. Do not conflate the two sets of thresholds.")

    print("\n" + "=" * W)


# ---------------------------------------------------------------------------
# .env updater
# ---------------------------------------------------------------------------

def update_env(thresholds: dict[str, float], *, dry_run: bool) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        print(f"\nWARNING: {env_path.resolve()} not found; cannot write changes.")
        return

    content = env_path.read_text(encoding="utf-8")
    changes: list[str] = []

    for key, value in thresholds.items():
        formatted = f"{value:.4f}" if abs(value) < 10 else f"{value:.2f}"
        new_line = f"{key}={formatted}"
        pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
        if pattern.search(content):
            replaced = pattern.sub(new_line, content)
            if replaced != content:
                content = replaced
                changes.append(f"  updated  {new_line}")
        else:
            content = content.rstrip("\n") + f"\n{new_line}\n"
            changes.append(f"  added    {new_line}")

    print("\n-- .env Changes " + "-" * 56)
    if not changes:
        print("  No changes needed (all values already match the file).")
        return

    for c in changes:
        print(c)

    if dry_run:
        print("\n  (dry-run — pass --update-env to write these changes)")
    else:
        env_path.write_text(content, encoding="utf-8")
        print(f"\n  Written to {env_path.resolve()}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--update-env",
        action="store_true",
        default=False,
        help="Write calibrated values to .env (default: print only)",
    )
    p.add_argument(
        "--backfill-vrp",
        action="store_true",
        default=False,
        help="Run VRP backfill before calibrating",
    )
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    dry_run = not args.update_env

    print("Checking data availability...")
    avail = await check_data_availability()

    if args.backfill_vrp:
        await run_vrp_backfill()
        avail = await check_data_availability()
    elif avail.get("vrp_backfill_needed", 0) > 0:
        print(
            f"\nNOTE: {avail['vrp_backfill_needed']} date(s) have atm_iv but no VRP. "
            "Run with --backfill-vrp to populate before calibrating VRP thresholds.\n"
        )

    print("Building feature dataset...")
    df = await build_feature_dataset()
    print(f"Feature dataset: {len(df)} trading days.")

    new_thresholds, notes = compute_thresholds(df)
    old_thresholds = DEFAULTS.copy()

    # Read current settings to reflect any already-applied values as "old"
    try:
        cfg = get_settings()
        old_thresholds = {
            "FNO_REGIME_VIX_SPIKE":    cfg.fno_regime_vix_spike,
            "FNO_REGIME_VIX_DROP":     cfg.fno_regime_vix_drop,
            "FNO_VIX_HIGH_THRESHOLD":  cfg.fno_vix_high_threshold,
            "FNO_REGIME_TREND_1D":     cfg.fno_regime_trend_1d,
            "FNO_REGIME_TREND_1W":     cfg.fno_regime_trend_1w,
            "FNO_REGIME_BREADTH_BULL": cfg.fno_regime_breadth_bull,
            "FNO_REGIME_BREADTH_BEAR": cfg.fno_regime_breadth_bear,
            "FNO_REGIME_VRP_CHEAP":    cfg.fno_regime_vrp_cheap,
            "FNO_REGIME_VRP_RICH":     cfg.fno_regime_vrp_rich,
        }
    except Exception:
        pass  # fall back to DEFAULTS if settings cannot be loaded

    print("Simulating regime frequencies...")
    old_freq = simulate_regime_frequencies(df, old_thresholds)
    new_freq = simulate_regime_frequencies(df, new_thresholds)

    print("Computing alignment score on closed signals...")
    alignment = await compute_alignment_score(df, new_thresholds)

    print_report(avail, df, old_thresholds, new_thresholds, notes, old_freq, new_freq, alignment)
    update_env(new_thresholds, dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(_main(parse_args()))
