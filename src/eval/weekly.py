"""Weekly postmortem analytics: P&L attribution, calibration drift, cost metrics."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import mean
from typing import Any
from uuid import uuid4

from sqlalchemy import text

log = logging.getLogger(__name__)


@dataclass
class WeekData:
    """All data for one week's postmortem analysis."""

    start: date
    end: date
    workflow_runs: list[dict] = field(default_factory=list)
    agent_runs: list[dict] = field(default_factory=list)
    all_predictions_count: int = 0          # all predictions regardless of outcome resolution
    resolved_predictions: list[dict] = field(default_factory=list)
    shadow_eval_scores: list[dict] = field(default_factory=list)


async def fetch_week_data(start: date, end: date, db_session_factory) -> WeekData:
    """Fetch all data needed for the weekly postmortem from the DB."""
    week = WeekData(start=start, end=end)
    try:
        async with db_session_factory() as db:
            # Workflow runs for the week
            result = await db.execute(
                text("""
                    SELECT id, workflow_name, status, cost_usd,
                           total_tokens, params, started_at
                    FROM workflow_runs
                    WHERE started_at >= :start AND started_at < :end
                      AND status = 'succeeded'
                    ORDER BY started_at
                """),
                {"start": start, "end": end + timedelta(days=1)},
            )
            week.workflow_runs = [dict(r._mapping) for r in result.fetchall()]

            # Agent runs for those workflow_runs
            if week.workflow_runs:
                run_ids = [str(r["id"]) for r in week.workflow_runs]
                result = await db.execute(
                    text("""
                        SELECT id, workflow_run_id, agent_name, persona_version,
                               model_used, cost_usd, input_tokens, output_tokens,
                               cache_read_tokens, status
                        FROM agent_runs
                        WHERE workflow_run_id = ANY(:ids)
                    """),
                    {"ids": run_ids},
                )
                week.agent_runs = [dict(r._mapping) for r in result.fetchall()]

            # Total prediction count (regardless of outcome resolution)
            count_result = await db.execute(
                text("""
                    SELECT COUNT(*) FROM agent_predictions
                    WHERE created_at >= :start AND created_at < :end
                """),
                {"start": start, "end": end + timedelta(days=1)},
            )
            week.all_predictions_count = count_result.scalar() or 0

            # Resolved predictions with outcomes
            result = await db.execute(
                text("""
                    SELECT ap.id, ap.workflow_run_id, ap.symbol_or_underlying,
                           ap.conviction, ap.expected_pnl_pct, ap.prompt_versions,
                           ap.model_used,
                           apo.realised_pnl_pct, apo.hit_target, apo.hit_stop,
                           apo.exit_reason
                    FROM agent_predictions ap
                    JOIN agent_predictions_outcomes apo ON apo.prediction_id = ap.id
                    WHERE ap.created_at >= :start AND ap.created_at < :end
                """),
                {"start": start, "end": end + timedelta(days=1)},
            )
            week.resolved_predictions = [dict(r._mapping) for r in result.fetchall()]

            # Shadow eval scores
            result = await db.execute(
                text("""
                    SELECT workflow_run_id, overall_score, calibration_score,
                           evidence_alignment_score, guardrail_proximity_score,
                           novelty_score, self_consistency_score, created_at
                    FROM agent_predictions_eval
                    WHERE created_at >= :start AND created_at < :end
                    ORDER BY created_at
                """),
                {"start": start, "end": end + timedelta(days=1)},
            )
            week.shadow_eval_scores = [dict(r._mapping) for r in result.fetchall()]

    except Exception as e:
        log.error(f"fetch_week_data failed: {e}")
    return week


def compute_pnl_attribution(week: WeekData) -> dict:
    """Decompose week's P&L into prompt-change buckets vs regime/random."""
    if not week.resolved_predictions:
        return {"week_total_pnl_pct": 0, "attribution": [], "unattributed_pp": 0}

    total_pnl = sum(
        float(p.get("realised_pnl_pct") or 0)
        for p in week.resolved_predictions
    )
    n_wins = sum(1 for p in week.resolved_predictions
                 if (p.get("realised_pnl_pct") or 0) > 0)
    n_total = len(week.resolved_predictions)

    return {
        "week_total_pnl_pct": total_pnl,
        "n_predictions": n_total,
        "n_wins": n_wins,
        "win_rate_pct": (n_wins / n_total * 100) if n_total else 0,
        "attribution": [],   # requires baseline comparison — populated in weekly_postmortem.py
        "unattributed_pp": total_pnl,
    }


def compute_calibration_drift(week: WeekData) -> dict:
    """Compute calibration drift across conviction bins."""
    bins = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]
    bin_data: dict[tuple, list[bool]] = {b: [] for b in bins}

    for p in week.resolved_predictions:
        conv = float(p.get("conviction") or 0)
        won = (p.get("realised_pnl_pct") or 0) > 0
        for lo, hi in bins:
            if lo <= conv < hi:
                bin_data[(lo, hi)].append(won)

    results = []
    for (lo, hi), outcomes in bin_data.items():
        n = len(outcomes)
        if n < 2:
            continue
        actual_rate = sum(outcomes) / n
        expected_rate = (lo + hi) / 2
        delta = actual_rate - expected_rate
        results.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": n,
            "actual_win_rate": actual_rate,
            "expected_win_rate": expected_rate,
            "delta": delta,
            "verdict": (
                "well_calibrated" if abs(delta) < 0.10
                else "overconfident" if expected_rate > actual_rate
                else "underconfident"
            ),
        })

    return {"ceo_judge": results}


def compute_cost_per_correct_prediction(week: WeekData) -> dict:
    """Compute cost efficiency metrics."""
    total_cost = sum(float(r.get("cost_usd") or 0) for r in week.workflow_runs)
    n_wins = sum(1 for p in week.resolved_predictions
                 if (p.get("realised_pnl_pct") or 0) > 0)
    n_total = len(week.resolved_predictions)

    by_agent: dict[str, Decimal] = defaultdict(Decimal)
    for ar in week.agent_runs:
        by_agent[ar["agent_name"]] += Decimal(str(ar.get("cost_usd") or 0))

    return {
        "total_llm_cost_usd": total_cost,
        "n_predictions": n_total,
        "n_wins": n_wins,
        "win_rate_pct": (n_wins / n_total * 100) if n_total else 0,
        "cost_per_prediction_usd": total_cost / n_total if n_total else None,
        "cost_per_win_usd": total_cost / n_wins if n_wins else None,
        "by_agent": {k: float(v) for k, v in by_agent.items()},
    }


def render_markdown_report(
    week_iso: str,
    week: WeekData,
    pnl_attribution: dict,
    calibration_drift: dict,
    cost_correct: dict,
    regression_results: list[dict],
    ab_results: list[dict],
) -> str:
    """Render the weekly postmortem as a markdown string."""
    lines = [
        f"# Laabh Weekly Postmortem — {week_iso}",
        "",
        f"**Period:** {week.start} to {week.end}",
        f"**Workflows run:** {len(week.workflow_runs)}",
        f"**Total LLM spend:** ${cost_correct['total_llm_cost_usd']:.2f}",
        f"**Total predictions:** {week.all_predictions_count}",
        f"**Resolved predictions:** {len(week.resolved_predictions)} of {week.all_predictions_count}",
        "",
        "---",
        "",
        "## Headline Numbers",
        "",
        "| Metric | This week |",
        "|---|---|",
        f"| Win rate | {cost_correct['win_rate_pct']:.0f}% ({cost_correct['n_wins']}/{cost_correct['n_predictions']}) |",
        f"| Total P&L | {pnl_attribution['week_total_pnl_pct']:+.1f}% |",
        f"| Cost per prediction | ${cost_correct['cost_per_prediction_usd']:.2f} |" if cost_correct['cost_per_prediction_usd'] else "| Cost per prediction | N/A |",
        f"| Cost per win | ${cost_correct['cost_per_win_usd']:.2f} |" if cost_correct['cost_per_win_usd'] else "| Cost per win | N/A |",
        "",
        "## Calibration Drift (CEO Judge)",
        "",
        "| Conviction bin | n | Actual win rate | Expected | Delta | Verdict |",
        "|---|---|---|---|---|---|",
    ]

    for row in calibration_drift.get("ceo_judge", []):
        lines.append(
            f"| {row['bin']} | {row['n']} | {row['actual_win_rate']:.0%} "
            f"| {row['expected_win_rate']:.0%} | {row['delta']:+.0%} | {row['verdict']} |"
        )

    lines += [
        "",
        "## Regression Suite Results",
        "",
        "| Seed | Tags | Status | Notes |",
        "|---|---|---|---|",
    ]
    for r in regression_results:
        status = "✅ passed" if r.get("passed") else "⚠️ DEGRADED"
        tags = ", ".join(r.get("tags", []))
        notes = "; ".join(r.get("failures", [])) or "—"
        lines.append(f"| {r.get('seed_id', '?')} | {tags} | {status} | {notes} |")

    if ab_results:
        lines += ["", "## A/B Replay Results"]
        for ab in ab_results:
            lines += [
                "",
                f"### `{ab['agent']}={ab['candidate_version']}` vs baseline (n={ab['n_replays']})",
                "",
                "| Metric | Value |",
                "|---|---|",
                f"| Decisions changed | {ab['n_decisions_changed']} of {ab['n_replays']} |",
                f"| Mean expected P&L delta | {ab.get('mean_expected_pnl_delta_pp', 0):+.2f}pp |",
            ]

    lines.append("")
    return "\n".join(lines)


async def send_telegram_digest(
    week_iso: str,
    week: WeekData,
    pnl_attribution: dict,
    calibration_drift: dict,
    telegram,
    chat_id: str,
) -> None:
    """Send a short weekly digest to Telegram."""
    if not telegram:
        return

    pnl = pnl_attribution.get("week_total_pnl_pct", 0)
    n_runs = len(week.workflow_runs)
    n_resolved = len(week.resolved_predictions)
    n_wins = pnl_attribution.get("n_wins", 0)
    win_rate = (n_wins / n_resolved * 100) if n_resolved else 0

    msg = (
        f"📊 Laabh {week_iso} Postmortem\n\n"
        f"P&L: {pnl:+.1f}% | Win rate: {win_rate:.0f}%\n"
        f"Workflows: {n_runs} | Predictions: {n_resolved}\n\n"
        f"Full report: reports/weekly/{week_iso}.md"
    )
    try:
        await telegram.send(chat_id=chat_id, text=msg)
    except Exception as e:
        log.warning(f"Telegram digest failed: {e}")


async def persist_prompt_version_results(
    ab_results: list[dict], week_iso: str, db_session_factory
) -> None:
    """Persist A/B results to prompt_version_results table."""
    async with db_session_factory() as db:
        for ab in ab_results:
            promotion = (
                ab["n_replays"] >= 5
                and (ab.get("mean_expected_pnl_delta_pp") or 0) > 1.0
            )
            await db.execute(
                text("""
                    INSERT INTO prompt_version_results (
                        id, agent_name, candidate_version, week_iso,
                        n_replays, n_decisions_changed, mean_expected_pnl_delta_pp,
                        raw_results, promotion_recommended, created_at
                    ) VALUES (
                        :id, :agent, :ver, :week,
                        :n, :changed, :delta,
                        :raw, :promo, NOW()
                    )
                    ON CONFLICT (agent_name, candidate_version, week_iso)
                    DO UPDATE SET
                        n_replays = EXCLUDED.n_replays,
                        n_decisions_changed = EXCLUDED.n_decisions_changed,
                        mean_expected_pnl_delta_pp = EXCLUDED.mean_expected_pnl_delta_pp,
                        raw_results = EXCLUDED.raw_results,
                        promotion_recommended = EXCLUDED.promotion_recommended
                """),
                {
                    "id": str(uuid4()),
                    "agent": ab["agent"],
                    "ver": ab["candidate_version"],
                    "week": week_iso,
                    "n": ab["n_replays"],
                    "changed": ab["n_decisions_changed"],
                    "delta": ab.get("mean_expected_pnl_delta_pp"),
                    "raw": json.dumps(ab.get("replays", [])),
                    "promo": promotion,
                },
            )
        await db.commit()
