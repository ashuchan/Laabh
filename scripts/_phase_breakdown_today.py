"""One-off: report today's F&O Phase 1/2/3 breakdown + Phase 3 LLM interactions + job_log."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

from dotenv import load_dotenv

load_dotenv(override=False)

from sqlalchemy import select, text

from src.db import session_scope
from src.models.fno_candidate import FNOCandidate
from src.models.instrument import Instrument
from src.models.llm_audit_log import LLMAuditLog


async def main() -> None:
    today = date.today()
    print(f"=== F&O pipeline breakdown for {today.isoformat()} ===\n")

    async with session_scope() as session:
        p1_rows = (await session.execute(
            select(
                FNOCandidate.passed_liquidity, FNOCandidate.atm_oi,
                FNOCandidate.atm_spread_pct, FNOCandidate.avg_volume_5d,
                FNOCandidate.created_at, Instrument.symbol,
            ).join(Instrument, Instrument.id == FNOCandidate.instrument_id)
            .where(FNOCandidate.run_date == today, FNOCandidate.phase == 1)
            .order_by(FNOCandidate.created_at)
        )).all()

        p2_rows = (await session.execute(
            select(
                FNOCandidate.news_score, FNOCandidate.sentiment_score,
                FNOCandidate.fii_dii_score, FNOCandidate.macro_align_score,
                FNOCandidate.convergence_score, FNOCandidate.composite_score,
                FNOCandidate.created_at, Instrument.symbol,
            ).join(Instrument, Instrument.id == FNOCandidate.instrument_id)
            .where(FNOCandidate.run_date == today, FNOCandidate.phase == 2)
            .order_by(FNOCandidate.composite_score.desc().nulls_last())
        )).all()

        p3_rows = (await session.execute(
            select(
                FNOCandidate.llm_decision, FNOCandidate.llm_thesis,
                FNOCandidate.iv_regime, FNOCandidate.oi_structure,
                FNOCandidate.technical_pass, FNOCandidate.created_at,
                Instrument.symbol,
            ).join(Instrument, Instrument.id == FNOCandidate.instrument_id)
            .where(FNOCandidate.run_date == today, FNOCandidate.phase == 3)
            .order_by(FNOCandidate.created_at)
        )).all()

        start_utc = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        end_utc = datetime.combine(today, datetime.max.time(), tzinfo=timezone.utc)
        audit_rows = (await session.execute(
            select(LLMAuditLog).where(
                LLMAuditLog.caller == "fno.thesis_synthesizer",
                LLMAuditLog.created_at >= start_utc,
                LLMAuditLog.created_at <= end_utc,
            ).order_by(LLMAuditLog.created_at)
        )).scalars().all()

        # job_log for today's F&O-related jobs (no ORM — raw SQL)
        job_rows = (await session.execute(
            text(
                "SELECT job_name, status, items_processed, duration_ms, "
                "error_message, created_at FROM job_log "
                "WHERE created_at >= :start AND created_at <= :end "
                "AND (job_name ILIKE 'fno%' OR job_name ILIKE '%phase%') "
                "ORDER BY created_at"
            ),
            {"start": start_utc, "end": end_utc},
        )).all()

    # ---- Phase 1
    p1_pass = sum(1 for r in p1_rows if r.passed_liquidity)
    p1_fail = sum(1 for r in p1_rows if r.passed_liquidity is False)
    print(f"--- Phase 1 (liquidity filter) ---")
    print(f"  total rows: {len(p1_rows)}    pass={p1_pass}  fail={p1_fail}")
    if p1_rows:
        last = max(p1_rows, key=lambda r: r.created_at)
        print(f"  last row written: {last.created_at.isoformat()} ({last.symbol})")
    if p1_pass:
        passing = sorted(
            [r for r in p1_rows if r.passed_liquidity],
            key=lambda r: (r.atm_oi or 0), reverse=True,
        )
        print(f"\n  Passing instruments (sorted by ATM OI):")
        print(f"  {'Symbol':<14}{'ATM OI':>12}{'Spread%':>10}{'5d Vol':>12}")
        for r in passing:
            spread = f"{float(r.atm_spread_pct):.4f}" if r.atm_spread_pct is not None else "n/a"
            print(f"  {r.symbol:<14}{(r.atm_oi or 0):>12,}{spread:>10}{(r.avg_volume_5d or 0):>12,}")

    # ---- Phase 2
    print(f"\n--- Phase 2 (catalyst scoring) ---")
    print(f"  total rows: {len(p2_rows)}")
    if p2_rows:
        print(f"\n  Top by composite_score:")
        print(f"  {'Symbol':<14}{'News':>7}{'Sent':>7}{'FII/DII':>9}"
              f"{'Macro':>7}{'Conv':>7}{'Composite':>11}")
        for r in p2_rows[:25]:
            f = lambda v: f"{float(v):.2f}" if v is not None else "—"
            print(f"  {r.symbol:<14}{f(r.news_score):>7}{f(r.sentiment_score):>7}"
                  f"{f(r.fii_dii_score):>9}{f(r.macro_align_score):>7}"
                  f"{f(r.convergence_score):>7}{f(r.composite_score):>11}")
        if len(p2_rows) > 25:
            print(f"  ... +{len(p2_rows) - 25} more")
    else:
        print(f"  (no Phase 2 rows for {today} — Phase 2 did not run "
              f"or produced zero candidates)")

    # ---- Phase 3
    print(f"\n--- Phase 3 (LLM thesis synthesis) ---")
    print(f"  total rows: {len(p3_rows)}")
    if p3_rows:
        decisions: dict[str, int] = {}
        for r in p3_rows:
            decisions[r.llm_decision or "(none)"] = (
                decisions.get(r.llm_decision or "(none)", 0) + 1
            )
        print(f"  decisions: {decisions}")
        for r in p3_rows:
            print(f"\n  {r.symbol}  →  {r.llm_decision}  "
                  f"(iv_regime={r.iv_regime}, oi={r.oi_structure}, "
                  f"tech_pass={r.technical_pass})")
            if r.llm_thesis:
                print(f"    thesis: {r.llm_thesis}")
    else:
        print(f"  (no Phase 3 rows — nothing was synthesized)")

    # ---- LLM audit
    print(f"\n=== LLM interactions (caller='fno.thesis_synthesizer') ===")
    print(f"total calls today: {len(audit_rows)}")
    for i, a in enumerate(audit_rows, 1):
        print(f"\n----- LLM call #{i} -----")
        print(f"  id          : {a.id}")
        print(f"  caller_ref  : {a.caller_ref_id}")
        print(f"  model       : {a.model}")
        print(f"  temperature : {float(a.temperature)}")
        print(f"  tokens      : in={a.tokens_in} out={a.tokens_out} "
              f"latency_ms={a.latency_ms}")
        print(f"  created_at  : {a.created_at.isoformat()}")
        print(f"\n  PROMPT:\n{a.prompt}")
        print(f"\n  RESPONSE:\n{a.response}")
        if a.response_parsed:
            print(f"\n  PARSED:\n{json.dumps(a.response_parsed, indent=2, default=str)}")

    # ---- job_log
    print(f"\n\n=== F&O / phase job_log entries for {today} ===")
    print(f"total: {len(job_rows)}")
    for j in job_rows:
        msg = (j.error_message or "")[:120]
        print(f"  {j.created_at.isoformat()}  {j.job_name:<35} "
              f"status={j.status:<10} items={j.items_processed or 0:<5} "
              f"duration_ms={j.duration_ms or 0}")
        if msg:
            print(f"      err: {msg}")


asyncio.run(main())
