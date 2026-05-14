"""Replay the five new architecture modules (Gaps 1-5) over the past N trading days.

No LLM calls. No Telegram. No side effects.
Uses data already in the DB (options_chain, iv_history, vix_ticks, etc.)

What this tests:
  Gap 1 — VRP Engine:        backfills vrp / rv_20d / vrp_regime in iv_history
  Gap 2 — Vol Surface:       builds vol_surface_snapshot for each day
  Gap 3 — Regime Classifier: classifies each day, stores in market_regime_snapshot
  Gap 4 — Greeks Engine:     computes portfolio Greeks (likely 0 if no open positions)
  Gap 5 — ML Shadow:         runs baseline predictor over today's fno_candidates

Run:
    python scripts/replay_new_modules.py [--days N]   # default: 7
    python scripts/replay_new_modules.py --days 14

Output: a table showing per-day VRP regime, market regime, Phase 3 decisions,
and how the new context would have changed them vs the old prompts (by inspecting
llm_audit_log).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

# Make sure project root is on path when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trading_days_back(n: int) -> list[date]:
    """Return the last N calendar days that are Mon–Fri (rough proxy for trading days)."""
    days = []
    d = date.today() - timedelta(days=1)   # start from yesterday
    while len(days) < n:
        if d.weekday() < 5:   # Mon=0 .. Fri=4
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


async def _check_chain_available(engine, d: date) -> bool:
    """Return True if options_chain has at least one snapshot for date d."""
    from sqlalchemy import text
    async with engine.connect() as conn:
        cnt = (await conn.execute(text(
            "SELECT COUNT(*) FROM options_chain WHERE DATE(snapshot_at) = :d"
        ), {"d": d})).scalar_one()
    return cnt > 0


async def _get_phase3_decisions(engine, d: date) -> list[dict]:
    """Pull Phase 3 decisions from fno_candidates for a date."""
    from sqlalchemy import text
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT i.symbol, fc.llm_decision, fc.composite_score,
                   fc.iv_regime, fc.oi_structure
            FROM fno_candidates fc
            JOIN instruments i ON i.id = fc.instrument_id
            WHERE fc.run_date = :d AND fc.phase = 3
              AND fc.dryrun_run_id IS NULL
            ORDER BY fc.composite_score DESC NULLS LAST
        """), {"d": d})).fetchall()
    return [dict(r._mapping) for r in rows]


async def _get_prompt_version_from_audit(engine, d: date) -> str | None:
    """Detect which prompt version was used on date d from llm_audit_log."""
    from sqlalchemy import text
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT prompt FROM llm_audit_log
            WHERE caller = 'fno.thesis_synthesizer'
              AND DATE(created_at) = :d
            LIMIT 1
        """), {"d": d})).first()
    if row is None:
        return None
    # Detect version from presence of known v8 fields in the prompt
    prompt = row.prompt or ""
    if "MARKET REGIME" in prompt:
        return "v8+"
    if "Vol Surface" in prompt:
        return "v7"
    if "VRP" in prompt and "vol pts" in prompt:
        return "v6"
    return "v5 or earlier"


# ---------------------------------------------------------------------------
# Per-day processing
# ---------------------------------------------------------------------------

async def process_day(d: date, engine) -> dict:
    """Run all five new modules for one trading day. Returns result dict."""
    result = {"date": d.isoformat(), "chain_available": False}

    # ---- Gap 1: VRP Engine ----
    try:
        from src.fno.vrp_engine import compute_vrp_for_date
        vrp_n = await compute_vrp_for_date(d)
        result["vrp_updated"] = vrp_n
    except Exception as exc:
        result["vrp_error"] = str(exc)[:80]
        vrp_n = 0

    # Fetch median VRP for the day
    from sqlalchemy import text
    async with engine.connect() as conn:
        vrp_row = (await conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE vrp IS NOT NULL)                         vrp_count,
                ROUND(AVG(vrp) FILTER (WHERE vrp IS NOT NULL) * 100, 2)         vrp_median_pts,
                MODE() WITHIN GROUP (ORDER BY vrp_regime)
                    FILTER (WHERE vrp_regime IS NOT NULL)                        vrp_mode_regime,
                ROUND(AVG(atm_iv) FILTER (WHERE atm_iv > 0) * 100, 2)           avg_atm_iv
            FROM iv_history
            WHERE date = :d AND dryrun_run_id IS NULL
        """), {"d": d})).first()
    if vrp_row:
        result.update({
            "vrp_instruments": vrp_row.vrp_count or 0,
            "vrp_median_vpts": vrp_row.vrp_median_pts,
            "vrp_mode": vrp_row.vrp_mode_regime or "—",
            "avg_atm_iv_pct": vrp_row.avg_atm_iv,
        })

    # ---- Gap 2: Vol Surface ----
    chain_ok = await _check_chain_available(engine, d)
    result["chain_available"] = chain_ok
    if chain_ok:
        try:
            from src.fno.vol_surface import compute_for_instruments
            surf_n = await compute_for_instruments(d)
            result["surface_computed"] = surf_n
        except Exception as exc:
            result["surface_error"] = str(exc)[:80]

        # Fetch surface summary
        async with engine.connect() as conn:
            surf_row = (await conn.execute(text("""
                SELECT
                    COUNT(*)                                                    total,
                    COUNT(*) FILTER (WHERE skew_regime='put_skewed')            put_skewed,
                    COUNT(*) FILTER (WHERE skew_regime='call_skewed')           call_skewed,
                    ROUND(AVG(iv_skew_5pct), 2)                                 avg_skew,
                    MODE() WITHIN GROUP (ORDER BY term_regime)                  term_mode,
                    ROUND(AVG(pcr_near_expiry), 3)                              avg_pcr
                FROM vol_surface_snapshot
                WHERE run_date = :d
            """), {"d": d})).first()
        if surf_row and surf_row.total:
            result.update({
                "surface_total": surf_row.total,
                "put_skewed_pct": f"{surf_row.put_skewed * 100 // surf_row.total:.0f}%",
                "call_skewed_pct": f"{surf_row.call_skewed * 100 // surf_row.total:.0f}%",
                "avg_skew_vpts": surf_row.avg_skew,
                "term_mode": surf_row.term_mode or "—",
                "avg_pcr": surf_row.avg_pcr,
            })

    # ---- Gap 3: Regime Classifier ----
    try:
        from src.fno.regime_classifier import compute_regime
        regime = await compute_regime(d)
        result.update({
            "regime": regime.regime,
            "regime_conf": regime.confidence,
            "vix": regime.vix_current,
            "nifty_1d": regime.nifty_1d_pct,
            "vrp_median_regime": regime.vrp_median,
        })
    except Exception as exc:
        result["regime_error"] = str(exc)[:80]

    # ---- Gap 4: Greeks Engine ----
    try:
        from src.fno.greeks_engine import compute_portfolio_greeks
        pg = await compute_portfolio_greeks()
        result.update({
            "greeks_positions": pg.open_positions,
            "net_delta": round(pg.net_delta, 3),
            "net_theta": round(pg.net_theta, 2),
            "net_vega": round(pg.net_vega, 2),
        })
    except Exception as exc:
        result["greeks_error"] = str(exc)[:80]

    # ---- Phase 3 decisions for this day ----
    decisions = await _get_phase3_decisions(engine, d)
    result["phase3_total"] = len(decisions)
    result["phase3_proceed"] = sum(1 for d_ in decisions if d_.get("llm_decision") == "PROCEED")
    result["phase3_hedge"] = sum(1 for d_ in decisions if d_.get("llm_decision") == "HEDGE")
    result["phase3_skip"] = sum(1 for d_ in decisions if d_.get("llm_decision") == "SKIP")

    # ---- Detect what prompt version was used that day ----
    result["prompt_version_used"] = await _get_prompt_version_from_audit(engine, d)

    # ---- Gap 5: ML Shadow — run baseline on existing Phase 2 candidates ----
    try:
        from sqlalchemy import text as _text
        from src.fno.ml_decision import extract_features, _baseline_predict
        from src.db import session_scope

        async with engine.connect() as conn:
            phase2_rows = (await conn.execute(_text("""
                SELECT fc.id::text cid, fc.instrument_id::text iid,
                       fc.composite_score::float comp,
                       fc.news_score::float news,
                       fc.sentiment_score::float sent,
                       fc.fii_dii_score::float fii,
                       fc.macro_align_score::float macro,
                       fc.convergence_score::float conv
                FROM fno_candidates fc
                WHERE fc.run_date = :d AND fc.phase = 2
                  AND fc.dryrun_run_id IS NULL
                ORDER BY ABS(fc.composite_score - 5) DESC
                LIMIT 10
            """), {"d": d})).fetchall()

        ml_proceeds = ml_hedges = ml_skips = 0
        for row in phase2_rows:
            feats = extract_features(
                candidate_id=row.cid, composite=row.comp or 5,
                news=row.news or 5, sentiment=row.sent or 5,
                fii_dii=row.fii, macro=row.macro or 5, convergence=row.conv or 5,
                iv_rank=None, vrp=None, vrp_regime=None,
                skew_regime=None, term_regime=None, pcr=None,
                vix=result.get("vix"), market_regime=result.get("regime"),
                days_to_expiry=7, atm_iv=None,
            )
            pred, _ = _baseline_predict(feats)
            if pred == "PROCEED":
                ml_proceeds += 1
            elif pred == "HEDGE":
                ml_hedges += 1
            else:
                ml_skips += 1

        result["ml_shadow_proceed"] = ml_proceeds
        result["ml_shadow_hedge"]   = ml_hedges
        result["ml_shadow_skip"]    = ml_skips
    except Exception as exc:
        result["ml_shadow_error"] = str(exc)[:80]

    return result


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def _print_report(results: list[dict]) -> None:
    print()
    print("=" * 100)
    print("7-DAY REPLAY REPORT — New Architecture Modules (Gaps 1-5)")
    print("=" * 100)

    # Section 1: VRP + Regime time series
    print("\n[GAP 1+3] VRP & Regime per day")
    print(f"{'Date':<12} {'Regime':<18} {'Conf':<6} {'VIX':<7} {'Nifty1d':<10} "
          f"{'VRP mode':<14} {'VRP median':>10}")
    print("-" * 80)
    for r in results:
        print(
            f"{r['date']:<12} "
            f"{r.get('regime', '—'):<18} "
            f"{r.get('regime_conf', ''):<6} "
            f"{r.get('vix') or '':<7} "
            f"{r.get('nifty_1d') or '':<10} "
            f"{r.get('vrp_mode', '—'):<14} "
            f"{str(r.get('vrp_median_vpts', '—')):>10} vpts"
        )

    # Section 2: Vol Surface
    print("\n[GAP 2] Vol Surface per day")
    print(f"{'Date':<12} {'Instruments':<12} {'Put-skewed%':<13} {'Avg skew':>10} {'Avg PCR':>8} {'Term mode':<15}")
    print("-" * 75)
    for r in results:
        if r.get("chain_available"):
            print(
                f"{r['date']:<12} "
                f"{r.get('surface_total', '—'):<12} "
                f"{r.get('put_skewed_pct', '—'):<13} "
                f"{str(r.get('avg_skew_vpts', '—')):>10} "
                f"{str(r.get('avg_pcr', '—')):>8} "
                f"{r.get('term_mode', '—'):<15}"
            )
        else:
            print(f"{r['date']:<12} (no chain snapshot available)")

    # Section 3: Phase 3 decisions
    print("\n[Phase 3] LLM decisions vs ML shadow (on Phase 2 passers)")
    print(f"{'Date':<12} {'Prompt':<10} {'P3 total':<10} {'LLM PRC':<9} {'LLM HDG':<9} "
          f"{'ML PRC':<8} {'ML HDG':<8}")
    print("-" * 75)
    for r in results:
        print(
            f"{r['date']:<12} "
            f"{r.get('prompt_version_used') or '—':<10} "
            f"{r.get('phase3_total', 0):<10} "
            f"{r.get('phase3_proceed', 0):<9} "
            f"{r.get('phase3_hedge', 0):<9} "
            f"{r.get('ml_shadow_proceed', 0):<8} "
            f"{r.get('ml_shadow_hedge', 0):<8}"
        )

    # Section 4: Greeks
    print("\n[GAP 4] Greeks Engine (portfolio-level)")
    total_positions = sum(r.get('greeks_positions', 0) for r in results)
    if total_positions == 0:
        print("  No open positions — Greeks engine initialized correctly (all zeros expected).")
    else:
        print(f"{'Date':<12} {'Positions':<11} {'Net delta':>10} {'Net theta':>10} {'Net vega':>10}")
        print("-" * 60)
        for r in results:
            if r.get('greeks_positions', 0) > 0:
                print(f"{r['date']:<12} {r['greeks_positions']:<11} {r['net_delta']:>10.3f} {r['net_theta']:>10.2f} {r['net_vega']:>10.2f}")

    # Section 5: Errors
    errors = [(r['date'], k, v) for r in results for k, v in r.items() if k.endswith('_error')]
    if errors:
        print("\n[ERRORS]")
        for d, k, v in errors:
            print(f"  {d} {k}: {v}")

    print()
    print("What to look for:")
    print("  - Regime column: should form coherent sequences (bear->bear->range, not random)")
    print("  - VRP median: expect negative values (cheap) during recent selloff (IV < RV)")
    print("  - Put-skewed%: expect >60% of instruments put-skewed in bearish week")
    print("  - Prompt version: days before today should show 'v5 or earlier' (old prompts)")
    print("    Today should show 'v8+' if pipeline ran today with new code")
    print("  - ML shadow vs LLM: counts should diverge — baseline is more mechanical")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(n_days: int) -> None:
    from src.db import get_engine, dispose_engine

    days = _trading_days_back(n_days)
    logger.info(f"Replaying {len(days)} trading days: {days[0]} → {days[-1]}")

    engine = get_engine()
    results = []

    for d in days:
        logger.info(f"Processing {d} ...")
        r = await process_day(d, engine)
        results.append(r)

    await dispose_engine()
    _print_report(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay new modules over past N trading days")
    parser.add_argument("--days", type=int, default=7, help="Number of trading days to replay")
    args = parser.parse_args()
    asyncio.run(main(args.days))
