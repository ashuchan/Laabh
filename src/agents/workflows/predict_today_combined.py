"""predict_today_combined workflow spec — the full morning prediction pipeline."""
from __future__ import annotations

from decimal import Decimal

from src.agents.runtime.spec import StageAgent, WorkflowSpec, WorkflowStage

PREDICT_TODAY_COMBINED_V1 = WorkflowSpec(
    name="predict_today_combined",
    version="v1",
    cost_ceiling_usd=Decimal("5.50"),   # +0.50 for shadow eval
    token_ceiling=165_000,              # +15k for shadow eval
    final_validators=("CEOJudgeOutputValidated",),
    default_params={
        "capital_base_mode": "deployed",
        "deployment_ratio": 0.30,
        "max_drawdown_tolerance_pct": 3.0,
        "target_daily_book_pnl_pct": 10.0,
    },
    stages=(
        WorkflowStage(
            stage_name="brain_triage",
            kind="sequential",
            agents=(
                StageAgent(
                    agent_name="brain_triage",
                    persona_version="v1",
                    output_key="triage",
                    on_iteration_failure="abort_workflow",
                ),
            ),
        ),
        # SHORT-CIRCUIT: if triage.skip_today, workflow ends here.
        WorkflowStage(
            stage_name="news_finder_per_candidate",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="news_finder",
                    persona_version="v1",
                    iteration_source="triage.fno_candidates+triage.equity_candidates",
                    on_iteration_failure="skip_one",
                    output_key="news_findings",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="news_editor_per_finding",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="news_editor",
                    persona_version="v1",
                    iteration_source="news_findings",
                    on_iteration_failure="skip_one",
                    output_key="editor_verdicts",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="explorer_pod_per_candidate",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="_explorer_pod",
                    persona_version="v1",
                    iteration_source="triage.fno_candidates+triage.equity_candidates",
                    on_iteration_failure="skip_one",
                    output_key="explorer_aggregates",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="experts_parallel",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="fno_expert",
                    persona_version="v1",
                    iteration_source="triage.fno_candidates",
                    on_iteration_failure="skip_one",
                    output_key="fno_candidates_full",
                ),
                StageAgent(
                    agent_name="equity_expert",
                    persona_version="v1",
                    iteration_source="triage.equity_candidates",
                    on_iteration_failure="skip_one",
                    output_key="equity_candidates_full",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="ceo_debate",
            kind="parallel",   # bull and bear run in parallel on the same cached data
            agents=(
                StageAgent(
                    agent_name="ceo_bull",
                    persona_version="v1",
                    output_key="bull_brief",
                    on_iteration_failure="abort_workflow",
                ),
                StageAgent(
                    agent_name="ceo_bear",
                    persona_version="v1",
                    output_key="bear_brief",
                    on_iteration_failure="abort_workflow",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="ceo_judge",
            kind="sequential",
            agents=(
                StageAgent(
                    agent_name="ceo_judge",
                    persona_version="v1",
                    output_key="judge_verdict",
                    on_iteration_failure="abort_workflow",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="shadow_evaluation",
            kind="sequential",
            agents=(
                StageAgent(
                    agent_name="shadow_evaluator",
                    persona_version="v1",
                    output_key="shadow_eval",
                    on_iteration_failure="skip_one",  # never abort for eval failures
                ),
            ),
        ),
    ),
)
