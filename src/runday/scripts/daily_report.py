"""End-of-day rollup queries for laabh-runday report."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from src.db import session_scope


async def build_report(report_date: date) -> dict[str, Any]:
    """Query all relevant tables and assemble the daily report dict.

    Reads from: chain_collection_log, fno_signals, fno_candidates,
                llm_audit_log, notifications, source_health, job_log, vix_ticks.
    No external calls — should complete in <5s for a single day.
    """
    today = report_date

    async with session_scope() as session:
        # ---------------------------------------------------------------
        # 1. Pipeline completeness
        # ---------------------------------------------------------------
        jobs_result = await session.execute(
            text(
                """
                SELECT job_name, status, COUNT(*) as runs,
                       MAX(created_at) as last_run
                FROM job_log
                WHERE DATE(created_at AT TIME ZONE 'UTC') = :today
                GROUP BY job_name, status
                ORDER BY last_run DESC
                """
            ),
            {"today": today.isoformat()},
        )
        job_rows = jobs_result.fetchall()

        # ---------------------------------------------------------------
        # 2. Chain health
        # ---------------------------------------------------------------
        chain_result = await session.execute(
            text(
                """
                SELECT
                    status,
                    COUNT(*) as cnt,
                    AVG(latency_ms) as avg_lat,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_lat
                FROM chain_collection_log
                WHERE DATE(attempted_at AT TIME ZONE 'UTC') = :today
                GROUP BY status
                """
            ),
            {"today": today.isoformat()},
        )
        chain_rows = chain_result.fetchall()

        nse_share_result = await session.execute(
            text(
                """
                SELECT COUNT(*) FROM chain_collection_log
                WHERE DATE(attempted_at AT TIME ZONE 'UTC') = :today
                AND final_source = 'nse'
                """
            ),
            {"today": today.isoformat()},
        )
        nse_count = nse_share_result.scalar() or 0

        issues_result = await session.execute(
            text(
                """
                SELECT issue_type, COUNT(*) as cnt,
                       SUM(CASE WHEN github_issue_url IS NOT NULL THEN 1 ELSE 0 END) as filed
                FROM chain_collection_issues
                WHERE DATE(detected_at AT TIME ZONE 'UTC') = :today
                GROUP BY issue_type
                """
            ),
            {"today": today.isoformat()},
        )
        issue_rows = issues_result.fetchall()

        # ---------------------------------------------------------------
        # 3. LLM activity
        # ---------------------------------------------------------------
        llm_result = await session.execute(
            text(
                """
                SELECT caller, COUNT(*), SUM(tokens_in), SUM(tokens_out),
                       AVG(latency_ms), PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
                FROM llm_audit_log
                WHERE DATE(created_at AT TIME ZONE 'UTC') = :today
                GROUP BY caller
                """
            ),
            {"today": today.isoformat()},
        )
        llm_rows = llm_result.fetchall()

        # ---------------------------------------------------------------
        # 4. Trading
        # ---------------------------------------------------------------
        signals_result = await session.execute(
            text(
                """
                SELECT
                    strategy_type,
                    status,
                    COUNT(*) as cnt,
                    SUM(final_pnl) as pnl_sum,
                    AVG(EXTRACT(EPOCH FROM (closed_at - filled_at))/60) as avg_hold_min
                FROM fno_signals
                WHERE DATE(proposed_at AT TIME ZONE 'UTC') = :today
                GROUP BY strategy_type, status
                ORDER BY strategy_type, status
                """
            ),
            {"today": today.isoformat()},
        )
        signal_rows = signals_result.fetchall()

        # Decision quality: closed signals with thesis
        quality_result = await session.execute(
            text(
                """
                SELECT
                    i.symbol,
                    fc.llm_thesis,
                    fs.strategy_type,
                    fs.status,
                    fs.final_pnl
                FROM fno_signals fs
                JOIN fno_candidates fc ON fs.candidate_id = fc.id
                JOIN instruments i ON fs.underlying_id = i.id
                WHERE DATE(fs.proposed_at AT TIME ZONE 'UTC') = :today
                AND fs.status LIKE 'closed%'
                AND fc.llm_thesis IS NOT NULL
                ORDER BY fs.final_pnl DESC NULLS LAST
                LIMIT 20
                """
            ),
            {"today": today.isoformat()},
        )
        quality_rows = quality_result.fetchall()

        # ---------------------------------------------------------------
        # 5. VIX ticks
        # ---------------------------------------------------------------
        vix_result = await session.execute(
            text(
                """
                SELECT COUNT(*), AVG(vix_value), MIN(vix_value), MAX(vix_value)
                FROM vix_ticks
                WHERE DATE(timestamp AT TIME ZONE 'UTC') = :today
                """
            ),
            {"today": today.isoformat()},
        )
        vix_row = vix_result.fetchone()

        # ---------------------------------------------------------------
        # 6. Candidates per phase
        # ---------------------------------------------------------------
        candidates_result = await session.execute(
            text(
                "SELECT phase, COUNT(*) FROM fno_candidates "
                "WHERE run_date = :today GROUP BY phase"
            ),
            {"today": today.isoformat()},
        )
        candidate_rows = {r[0]: r[1] for r in candidates_result.fetchall()}

        # ---------------------------------------------------------------
        # 7. Source degradation events
        # ---------------------------------------------------------------
        source_result = await session.execute(
            text("SELECT source, status, consecutive_errors FROM source_health")
        )
        source_rows = source_result.fetchall()

    # ---------------------------------------------------------------
    # Assemble the report dict
    # ---------------------------------------------------------------

    # Pipeline completeness
    job_by_name: dict[str, list[dict[str, Any]]] = {}
    for job_name, status, runs, last_run in job_rows:
        job_by_name.setdefault(job_name, []).append(
            {"status": status, "runs": runs, "last_run": str(last_run)}
        )

    scheduled_jobs = [
        "fno_tier_refresh",
        "fno_phase1",
        "fno_phase2",
        "fno_phase3",
        "fno_morning_brief",
        "fno_phase4_entry",
        "fno_phase4_manage",
        "fno_iv_history",
        "fno_ban_list",
        "fno_review_loop",
    ]
    ran_jobs = set(job_by_name.keys())

    pipeline_completeness = {
        "total_scheduled": len(scheduled_jobs),
        "ran": len(ran_jobs & set(scheduled_jobs)),
        "skipped": sorted(set(scheduled_jobs) - ran_jobs),
        "jobs": job_by_name,
    }

    # Chain health
    chain_by_status: dict[str, dict[str, Any]] = {}
    chain_total = 0
    for status, cnt, avg_lat, p95_lat in chain_rows:
        chain_by_status[status] = {
            "count": cnt,
            "avg_latency_ms": round(avg_lat) if avg_lat else None,
            "p95_latency_ms": round(p95_lat) if p95_lat else None,
        }
        chain_total += cnt

    ok_count = chain_by_status.get("ok", {}).get("count", 0)
    fb_count = chain_by_status.get("fallback_used", {}).get("count", 0)
    ms_count = chain_by_status.get("missed", {}).get("count", 0)

    chain_health = {
        "total": chain_total,
        "ok": ok_count,
        "fallback": fb_count,
        "missed": ms_count,
        "ok_pct": round(ok_count / chain_total * 100, 2) if chain_total else 0,
        "fallback_pct": round(fb_count / chain_total * 100, 2) if chain_total else 0,
        "missed_pct": round(ms_count / chain_total * 100, 2) if chain_total else 0,
        "nse_share_pct": round(nse_count / chain_total * 100, 2) if chain_total else 0,
        "by_status": chain_by_status,
        "issues": [
            {"type": r[0], "count": r[1], "filed": r[2]} for r in issue_rows
        ],
    }

    # LLM activity
    callers = []
    total_tokens_in = 0
    total_tokens_out = 0
    total_llm_rows = 0
    for caller, count, t_in, t_out, avg_lat, p95_lat in llm_rows:
        callers.append(
            {
                "caller": caller,
                "row_count": count,
                "tokens_in": t_in or 0,
                "tokens_out": t_out or 0,
                "avg_latency_ms": round(avg_lat) if avg_lat else None,
                "p95_latency_ms": round(p95_lat) if p95_lat else None,
            }
        )
        total_tokens_in += t_in or 0
        total_tokens_out += t_out or 0
        total_llm_rows += count

    cost_usd = (total_tokens_in * 3.0 + total_tokens_out * 15.0) / 1_000_000
    llm_activity = {
        "callers": callers,
        "total_rows": total_llm_rows,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "estimated_cost_usd": round(cost_usd, 4),
    }

    # Trading
    by_strategy: dict[str, dict[str, Any]] = {}
    by_status_count: dict[str, int] = {}
    total_pnl = 0.0
    for strategy, status, cnt, pnl_sum, avg_hold in signal_rows:
        by_strategy.setdefault(strategy, {"count": 0, "pnl": 0.0})
        by_strategy[strategy]["count"] += cnt
        by_strategy[strategy]["pnl"] += float(pnl_sum or 0)
        by_status_count[status] = by_status_count.get(status, 0) + cnt
        total_pnl += float(pnl_sum or 0)

    decision_quality = []
    for sym, thesis, strategy, status, pnl in quality_rows:
        decision_quality.append(
            {
                "symbol": sym,
                "strategy": strategy,
                "status": status,
                "final_pnl": float(pnl) if pnl is not None else None,
                "thesis_excerpt": (thesis[:200] + "…") if thesis and len(thesis) > 200 else thesis,
            }
        )

    time_in_trade_result: list[float] = []
    trading = {
        "proposed": by_status_count.get("proposed", 0),
        "filled": by_status_count.get("paper_filled", 0) + by_status_count.get("active", 0),
        "scaled_out": by_status_count.get("scaled_out_50", 0),
        "closed_target": by_status_count.get("closed_target", 0),
        "closed_stop": by_status_count.get("closed_stop", 0),
        "closed_time": by_status_count.get("closed_time", 0),
        "day_pnl": round(total_pnl, 2),
        "by_strategy": by_strategy,
        "by_status": by_status_count,
        "decision_quality": decision_quality,
    }

    # VIX
    vix_stats = {}
    if vix_row and vix_row[0]:
        count, avg_v, min_v, max_v = vix_row
        vix_stats = {
            "tick_count": count,
            "avg_value": round(float(avg_v), 4),
            "min_value": round(float(min_v), 4),
            "max_value": round(float(max_v), 4),
        }

    # Candidates
    candidates = {f"phase{p}": count for p, count in candidate_rows.items()}

    # Source health
    source_health = [
        {"source": r[0], "status": r[1], "consecutive_errors": r[2]} for r in source_rows
    ]

    # Surprises
    surprises = _detect_surprises(chain_health, trading, pipeline_completeness)

    return {
        "date": today.isoformat(),
        "pipeline_completeness": pipeline_completeness,
        "chain_health": chain_health,
        "llm_activity": llm_activity,
        "trading": trading,
        "candidates": candidates,
        "vix_stats": vix_stats,
        "source_health": source_health,
        "surprises": surprises,
    }


def _detect_surprises(
    chain: dict[str, Any],
    trading: dict[str, Any],
    pipeline: dict[str, Any],
) -> list[str]:
    """Flag anomalous conditions for the Surprises section."""
    surprises = []

    # NSE share < 80% overall
    if chain.get("nse_share_pct", 100) < 80:
        surprises.append(
            f"NSE share dropped to {chain['nse_share_pct']:.1f}% (threshold: 80%)"
        )

    # Missed rate > 5%
    if chain.get("missed_pct", 0) > 5:
        surprises.append(
            f"Chain missed rate {chain['missed_pct']:.1f}% exceeded 5% threshold"
        )

    # Skipped scheduled jobs
    skipped = pipeline.get("skipped", [])
    if skipped:
        surprises.append(f"Skipped jobs: {', '.join(skipped)}")

    return surprises


def format_markdown_report(data: dict[str, Any]) -> str:
    """Format the report data as a Markdown string."""
    date_str = data.get("date", "unknown")
    lines = [
        f"# Laabh Daily Report — {date_str}",
        "",
        f"_Generated at {datetime.utcnow().isoformat()}Z_",
        "",
        "## Pipeline Completeness",
    ]

    pc = data.get("pipeline_completeness", {})
    lines.append(
        f"- Ran: {pc.get('ran', 0)}/{pc.get('total_scheduled', 0)} scheduled jobs"
    )
    skipped = pc.get("skipped", [])
    if skipped:
        lines.append(f"- Skipped: {', '.join(skipped)}")
    lines.append("")

    lines.append("## Data Ingestion Health")
    ch = data.get("chain_health", {})
    lines.extend(
        [
            f"- Chain total: {ch.get('total', 0)} attempts",
            f"- Ok: {ch.get('ok_pct', 0):.1f}%  Fallback: {ch.get('fallback_pct', 0):.1f}%  Missed: {ch.get('missed_pct', 0):.1f}%",
            f"- NSE share: {ch.get('nse_share_pct', 0):.1f}%",
            "",
        ]
    )

    lines.append("## LLM Activity")
    llm = data.get("llm_activity", {})
    lines.extend(
        [
            f"- Total calls: {llm.get('total_rows', 0)}",
            f"- Tokens in: {llm.get('total_tokens_in', 0):,}  out: {llm.get('total_tokens_out', 0):,}",
            f"- Estimated cost: ~${llm.get('estimated_cost_usd', 0):.4f}",
            "",
        ]
    )

    lines.append("## Trading")
    tr = data.get("trading", {})
    lines.extend(
        [
            f"- Proposed: {tr.get('proposed', 0)}  Filled: {tr.get('filled', 0)}",
            f"- Closed (target/stop/time): {tr.get('closed_target', 0)}/{tr.get('closed_stop', 0)}/{tr.get('closed_time', 0)}",
            f"- Day P&L: ₹{tr.get('day_pnl', 0):,.0f} (paper)",
            "",
        ]
    )

    dq = tr.get("decision_quality", [])
    if dq:
        lines.append("### Decision Quality")
        lines.append("")
        lines.append("| Symbol | Strategy | Status | P&L | Thesis |")
        lines.append("|--------|----------|--------|-----|--------|")
        for row in dq:
            pnl = f"₹{row['final_pnl']:,.0f}" if row["final_pnl"] is not None else "n/a"
            thesis = (row.get("thesis_excerpt") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {row['symbol']} | {row['strategy']} | {row['status']} | {pnl} | {thesis} |"
            )
        lines.append("")

    surprises = data.get("surprises", [])
    if surprises:
        lines.append("## Surprises")
        for s in surprises:
            lines.append(f"- ⚠️ {s}")
        lines.append("")

    return "\n".join(lines)
