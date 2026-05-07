"""WORKFLOW_REGISTRY — all named workflow specs."""
from __future__ import annotations

from decimal import Decimal

from src.agents.runtime.spec import StageAgent, WorkflowSpec, WorkflowStage
from src.agents.workflows.predict_today_combined import PREDICT_TODAY_COMBINED_V1

# F&O-only variant
PREDICT_TODAY_FNO_V1 = WorkflowSpec(
    name="predict_today_fno",
    version="v1",
    cost_ceiling_usd=Decimal("3.50"),
    token_ceiling=100_000,
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
            agents=(StageAgent(agent_name="brain_triage", persona_version="v1",
                               output_key="triage", on_iteration_failure="abort_workflow"),),
        ),
        WorkflowStage(
            stage_name="news_finder_per_fno_candidate",
            kind="parallel",
            agents=(StageAgent(agent_name="news_finder", persona_version="v1",
                               iteration_source="triage.fno_candidates",
                               output_key="news_findings"),),
        ),
        WorkflowStage(
            stage_name="fno_expert",
            kind="parallel",
            agents=(StageAgent(agent_name="fno_expert", persona_version="v1",
                               iteration_source="triage.fno_candidates",
                               output_key="fno_candidates_full"),),
        ),
        WorkflowStage(
            stage_name="ceo_debate",
            kind="parallel",
            agents=(
                StageAgent(agent_name="ceo_bull", persona_version="v1",
                           output_key="bull_brief", on_iteration_failure="abort_workflow"),
                StageAgent(agent_name="ceo_bear", persona_version="v1",
                           output_key="bear_brief", on_iteration_failure="abort_workflow"),
            ),
        ),
        WorkflowStage(
            stage_name="ceo_judge",
            kind="sequential",
            agents=(StageAgent(agent_name="ceo_judge", persona_version="v1",
                               output_key="judge_verdict",
                               on_iteration_failure="abort_workflow"),),
        ),
        WorkflowStage(
            stage_name="shadow_evaluation",
            kind="sequential",
            agents=(StageAgent(agent_name="shadow_evaluator", persona_version="v1",
                               output_key="shadow_eval", on_iteration_failure="skip_one"),),
        ),
    ),
)

EVALUATE_YESTERDAY_V1 = WorkflowSpec(
    name="evaluate_yesterday",
    version="v1",
    cost_ceiling_usd=Decimal("1.00"),
    token_ceiling=30_000,
    final_validators=(),
    default_params={},
    stages=(
        WorkflowStage(
            stage_name="shadow_evaluation",
            kind="sequential",
            agents=(StageAgent(agent_name="shadow_evaluator", persona_version="v1",
                               output_key="shadow_eval"),),
        ),
    ),
)

WORKFLOW_REGISTRY: dict[str, WorkflowSpec] = {
    "predict_today_combined": PREDICT_TODAY_COMBINED_V1,
    "predict_today_fno": PREDICT_TODAY_FNO_V1,
    "evaluate_yesterday": EVALUATE_YESTERDAY_V1,
}

__all__ = ["WORKFLOW_REGISTRY", "PREDICT_TODAY_COMBINED_V1", "PREDICT_TODAY_FNO_V1",
           "EVALUATE_YESTERDAY_V1"]
