"""Declarative specs for agents and workflows — immutable, loaded at startup."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class AgentSpec:
    """Declarative configuration for one agent persona.

    Immutable for the duration of a workflow run. Loaded from PERSONA_MANIFEST.
    """

    name: str
    persona_version: str
    model: str
    fallback_model: str | None
    tools: tuple[str, ...]
    output_tool: str | None
    max_input_tokens: int
    max_output_tokens: int
    temperature: float = 0.0
    max_retries_transient: int = 3
    max_retries_validation: int = 2
    on_failure: Literal["skip", "abort", "degrade"] = "abort"
    timeout_seconds: int = 60
    cache_system: bool = True
    stream_response: bool = False
    cost_class: Literal["cheap", "medium", "expensive"] = "medium"
    output_validator: str | None = None


@dataclass(frozen=True)
class StageAgent:
    """One agent slot within a WorkflowStage.

    The same persona may appear multiple times in a stage (e.g. fno_expert
    called once per F&O candidate via iteration_source).
    """

    agent_name: str
    persona_version: str = "v1"
    iteration_source: str | None = None
    on_iteration_failure: Literal["skip_one", "abort_stage", "abort_workflow"] = "skip_one"
    output_key: str = ""


@dataclass(frozen=True)
class WorkflowStage:
    """One stage of a workflow. Runs to completion before the next stage."""

    stage_name: str
    kind: Literal["sequential", "parallel", "conditional"]
    agents: tuple[StageAgent, ...]
    condition: str | None = None


@dataclass(frozen=True)
class WorkflowSpec:
    """Declarative spec for one workflow type."""

    name: str
    version: str
    stages: tuple[WorkflowStage, ...]
    default_params: dict[str, Any] = field(default_factory=dict)
    cost_ceiling_usd: Decimal = Decimal("5.0")
    token_ceiling: int = 150_000
    final_validators: tuple[str, ...] = ()


@dataclass
class ToolContext:
    """Runtime context injected into every tool executor."""

    workflow_run_id: str
    agent_run_id: str
    agent_name: str
    db: Any                          # AsyncSession
    redis: Any                       # Redis
    as_of: Any                       # datetime
    is_replay: bool = False
    instrument_context: dict[str, Any] = field(default_factory=dict)
    cost_so_far_usd: Decimal = field(default_factory=lambda: Decimal("0"))


@dataclass
class AgentRunResult:
    """Returned by WorkflowRunner._invoke_agent. Persisted to agent_runs."""

    agent_run_id: str
    agent_name: str
    persona_version: str
    model_used: str
    status: Literal["succeeded", "skipped", "failed", "rejected_by_guardrail"]
    output: dict | None
    raw_output: dict | None
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    duration_ms: int
    error: str | None = None
    validation_errors: list[str] = field(default_factory=list)
    llm_audit_log_id: str | None = None
    tool_calls_made: list[dict] = field(default_factory=list)


@dataclass
class WorkflowRunResult:
    """Returned by WorkflowRunner.run()."""

    workflow_run_id: str
    workflow_name: str
    status: str
    status_extended: str | None
    cost_usd: Decimal
    total_tokens: int
    agent_run_results: list[AgentRunResult]
    predictions: list[dict]
    validator_outcomes: list[dict]
    error: str | None = None
    short_circuit_reason: str | None = None


@dataclass
class RunnerConfig:
    """Tunable runtime config — overridable via env or CLI."""

    default_temperature: float = 0.0
    default_timeout_seconds: int = 60
    opus_timeout_seconds: int = 180
    transient_retry_max_backoff_s: int = 30
    enable_streaming: bool = True
    enable_caching: bool = True
    telegram_alert_on_failure: bool = True
    telegram_alert_on_caveat: bool = True
    cost_alert_threshold_usd: Decimal = field(default_factory=lambda: Decimal("3.0"))
    orphan_reconciliation_interval_minutes: int = 30
    replay_serve_from_cache_default: bool = True
    telegram_chat_id: str = ""


@dataclass
class _AgentInvocation:
    """Internal: one resolved agent call (after iteration_source expansion)."""

    stage_agent: StageAgent
    item: Any
    item_index: int


@dataclass
class WorkflowContext:
    """Mutable runtime state for one workflow execution."""

    workflow_run_id: str
    workflow_spec: WorkflowSpec
    params: dict[str, Any]
    cost_so_far_usd: Decimal
    tokens_so_far: int
    agent_run_results: list[AgentRunResult]
    stage_outputs: dict[str, Any]
    db_session_factory: Any
    redis: Any
    anthropic: Any
    as_of: Any
    telegram: Any = None
    is_replay: bool = False
    persona_version_overrides: dict[str, str] = field(default_factory=dict)
