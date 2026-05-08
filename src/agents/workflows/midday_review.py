"""midday_review workflow — the single midday CEO touchpoint.

Pipeline:
  1. For each symbol that's either a current open position OR a high-velocity
     watch-symbol from the snapshot, run cheap intraday explorer collectors
     (Haiku) — trend + sentiment_drift.
  2. For each, compose a `smart_report` (also Haiku) — terse status with
     since-morning deltas + recommendation.
  3. ONE midday_ceo (Opus) call reads the morning verdict + all smart_reports
     and issues per-position calls (STAY / SCALE_DOWN / EXIT / ADD_PILOT).

Cost target: ≤ $1.00 per midday run versus ≈$2.50 for a fresh full pipeline.
"""
from __future__ import annotations

from decimal import Decimal

from src.agents.runtime.spec import StageAgent, WorkflowSpec, WorkflowStage

MIDDAY_REVIEW_V1 = WorkflowSpec(
    name="midday_review",
    version="v1",
    cost_ceiling_usd=Decimal("1.20"),
    token_ceiling=40_000,
    final_validators=(),
    default_params={
        "max_drawdown_tolerance_pct": 3.0,
        "target_daily_book_pnl_pct": 10.0,
    },
    stages=(
        WorkflowStage(
            stage_name="intraday_trend",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="explorer_trend",
                    persona_version="v_intraday",
                    iteration_source="midday.watch_symbols",
                    on_iteration_failure="skip_one",
                    output_key="intraday_trend",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="intraday_sentiment",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="explorer_sentiment_drift",
                    persona_version="v_intraday",
                    iteration_source="midday.watch_symbols",
                    on_iteration_failure="skip_one",
                    output_key="intraday_sentiment",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="smart_reports",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="smart_report",
                    persona_version="v1",
                    iteration_source="midday.watch_symbols",
                    on_iteration_failure="skip_one",
                    output_key="smart_reports",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="midday_ceo",
            kind="sequential",
            agents=(
                StageAgent(
                    agent_name="midday_ceo",
                    persona_version="v1",
                    output_key="midday_verdict",
                    on_iteration_failure="abort_workflow",
                ),
            ),
        ),
    ),
)
