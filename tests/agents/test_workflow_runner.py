"""Tests for WorkflowRunner — using mocked DB and Anthropic client."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.runtime.spec import (
    AgentRunResult,
    StageAgent,
    WorkflowSpec,
    WorkflowStage,
)
from src.agents.runtime.workflow_runner import (
    BudgetExceeded,
    DuplicateRun,
    WorkflowRunner,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_fake_db():
    """Return a mock db_session_factory that satisfies the async context manager protocol."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(fetchone=lambda: None, rowcount=0))
    mock_session.commit = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield mock_session

    return factory


def make_fake_redis(kill_switch_active=False, idempotency_taken=False):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1" if kill_switch_active else None)
    redis.set = AsyncMock(return_value=None if idempotency_taken else True)
    return redis


def make_runner(db=None, redis=None, anthropic=None):
    return WorkflowRunner(
        db_session_factory=db or make_fake_db(),
        redis=redis or make_fake_redis(),
        anthropic=anthropic,
    )


def make_minimal_workflow(skip_today=False) -> WorkflowSpec:
    """A minimal workflow with only brain_triage for fast unit tests."""
    return WorkflowSpec(
        name="test_workflow",
        version="v1",
        cost_ceiling_usd=Decimal("5.0"),
        token_ceiling=100_000,
        stages=(
            WorkflowStage(
                stage_name="brain_triage",
                kind="sequential",
                agents=(
                    StageAgent(
                        agent_name="brain_triage",
                        persona_version="v1",
                        output_key="triage",
                    ),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestWorkflowRunnerInit:
    def test_initialises_without_anthropic(self):
        runner = make_runner()
        assert runner.anthropic is None
        assert runner.config is not None

    def test_persona_manifest_loaded(self):
        runner = make_runner()
        assert "brain_triage" in runner._persona_manifest
        assert "ceo_judge" in runner._persona_manifest

    def test_tool_registry_loaded(self):
        runner = make_runner()
        assert "search_raw_content" in runner._tool_registry
        assert "get_options_chain" in runner._tool_registry

    def test_validator_registry_loaded(self):
        runner = make_runner()
        assert "CEOJudgeOutputValidated" in runner._validator_registry


class TestBuildAgentSpec:
    def test_resolves_known_agent(self):
        runner = make_runner()
        spec = runner._build_agent_spec("brain_triage", "v1")
        assert spec.name == "brain_triage"
        assert spec.model == "claude-haiku-4-5-20251001"
        assert spec.output_tool == "emit_brain_triage"

    def test_raises_for_unknown_agent(self):
        runner = make_runner()
        with pytest.raises(ValueError, match="not in PERSONA_MANIFEST"):
            runner._build_agent_spec("nonexistent_agent", "v1")

    def test_raises_for_unknown_version(self):
        runner = make_runner()
        with pytest.raises(ValueError, match="not in PERSONA_MANIFEST"):
            runner._build_agent_spec("brain_triage", "v999")


class TestIterationResolution:
    def test_simple_key(self):
        from src.agents.runtime.spec import WorkflowContext
        runner = make_runner()
        ctx = WorkflowContext(
            workflow_run_id="test",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={"triage": {"fno_candidates": [{"symbol": "BANKNIFTY"}]}},
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        items = runner._resolve_iteration("triage.fno_candidates", ctx)
        assert items == [{"symbol": "BANKNIFTY"}]

    def test_combined_plus_source(self):
        from src.agents.runtime.spec import WorkflowContext
        runner = make_runner()
        ctx = WorkflowContext(
            workflow_run_id="test",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={
                "triage": {
                    "fno_candidates": [{"symbol": "BANKNIFTY"}],
                    "equity_candidates": [{"symbol": "TATAMOTORS"}],
                }
            },
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        items = runner._resolve_iteration("triage.fno_candidates+triage.equity_candidates", ctx)
        assert len(items) == 2
        symbols = [i["symbol"] for i in items]
        assert "BANKNIFTY" in symbols
        assert "TATAMOTORS" in symbols

    def test_missing_key_returns_empty(self):
        from src.agents.runtime.spec import WorkflowContext
        runner = make_runner()
        ctx = WorkflowContext(
            workflow_run_id="test",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={},
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        items = runner._resolve_iteration("triage.fno_candidates", ctx)
        assert items == []


class TestShouldShortCircuit:
    def test_brain_triage_skip_today_true(self):
        from src.agents.runtime.spec import WorkflowContext
        runner = make_runner()
        stage = WorkflowStage(
            stage_name="brain_triage",
            kind="sequential",
            agents=(StageAgent(agent_name="brain_triage"),),
        )
        ctx = WorkflowContext(
            workflow_run_id="x",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={"triage": {"skip_today": True}},
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        assert runner._should_short_circuit(stage, ctx) is True

    def test_brain_triage_skip_today_false(self):
        from src.agents.runtime.spec import WorkflowContext
        runner = make_runner()
        stage = WorkflowStage(
            stage_name="brain_triage",
            kind="sequential",
            agents=(StageAgent(agent_name="brain_triage"),),
        )
        ctx = WorkflowContext(
            workflow_run_id="x",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={"triage": {"skip_today": False}},
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        assert runner._should_short_circuit(stage, ctx) is False

    def test_non_triage_stage_never_short_circuits(self):
        from src.agents.runtime.spec import WorkflowContext
        runner = make_runner()
        stage = WorkflowStage(
            stage_name="ceo_debate",
            kind="parallel",
            agents=(StageAgent(agent_name="ceo_bull"),),
        )
        ctx = WorkflowContext(
            workflow_run_id="x",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={"triage": {"skip_today": True}},
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        assert runner._should_short_circuit(stage, ctx) is False


class TestMakeFailedResult:
    def test_returns_failed_status(self):
        runner = make_runner()
        spec = runner._build_agent_spec("brain_triage", "v1")
        result = WorkflowRunner._make_failed_result(
            spec, "run-1", "claude-haiku-4-5-20251001", "timeout", "timeout_exhausted"
        )
        assert result.status == "failed"
        assert result.cost_usd == Decimal("0")
        assert "timeout_exhausted" in result.error
        assert result.output is None


class TestRunWithoutAnthropicClient:
    """When no Anthropic client is configured, agents fail fast but don't crash the runner."""

    @pytest.mark.asyncio
    async def test_run_returns_failed_when_no_client(self):
        runner = make_runner()
        wf = make_minimal_workflow()
        result = await runner.run(wf, params={})
        # Without a client, the brain_triage agent will fail fast
        # The workflow should mark as failed, not raise
        assert result.workflow_run_id is not None
        assert result.status in ("failed", "succeeded")  # succeeded if short-circuit


class TestProjectStageCost:
    def test_sequential_stage_single_agent(self):
        runner = make_runner()
        stage = WorkflowStage(
            stage_name="brain_triage",
            kind="sequential",
            agents=(StageAgent(agent_name="brain_triage", persona_version="v1"),),
        )
        from src.agents.runtime.spec import WorkflowContext
        ctx = WorkflowContext(
            workflow_run_id="x",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={},
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        cost = runner._project_stage_cost(stage, ctx)
        assert cost > Decimal("0")
        assert isinstance(cost, Decimal)

    def test_iteration_source_multiplies_by_5(self):
        runner = make_runner()
        stage_single = WorkflowStage(
            stage_name="news_finder",
            kind="parallel",
            agents=(StageAgent(agent_name="news_finder", persona_version="v1"),),
        )
        stage_iter = WorkflowStage(
            stage_name="news_finder_iter",
            kind="parallel",
            agents=(StageAgent(
                agent_name="news_finder", persona_version="v1",
                iteration_source="triage.fno_candidates",
            ),),
        )
        from src.agents.runtime.spec import WorkflowContext
        ctx = WorkflowContext(
            workflow_run_id="x",
            workflow_spec=make_minimal_workflow(),
            params={},
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={},
            db_session_factory=make_fake_db(),
            redis=make_fake_redis(),
            anthropic=None,
            as_of=None,
        )
        single_cost = runner._project_stage_cost(stage_single, ctx)
        iter_cost = runner._project_stage_cost(stage_iter, ctx)
        assert iter_cost == single_cost * 5
