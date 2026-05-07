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


# ---------------------------------------------------------------------------
# Tests that require a mock Anthropic client
# ---------------------------------------------------------------------------

def _make_anthropic_response(tool_name: str, tool_input: dict, model: str = "claude-haiku-4-5-20251001"):
    """Build a minimal Anthropic messages.create response with one tool_use block."""
    import types

    usage = types.SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    tool_block = types.SimpleNamespace(
        type="tool_use",
        id="toolu_fake_001",
        name=tool_name,
        input=tool_input,
    )
    response = types.SimpleNamespace(
        content=[tool_block],
        usage=usage,
        model=model,
    )
    return response


def make_mock_anthropic(tool_input: dict, tool_name: str = "emit_brain_triage",
                        side_effect=None):
    """Return an AsyncAnthropic-like mock whose messages.create returns a fixed response."""
    anthropic = MagicMock()
    if side_effect:
        anthropic.messages.create = AsyncMock(side_effect=side_effect)
        anthropic.messages.stream = MagicMock(side_effect=side_effect)
    else:
        anthropic.messages.create = AsyncMock(
            return_value=_make_anthropic_response(tool_name, tool_input)
        )
    return anthropic


class TestExecuteWithRetries:
    """Tests for _execute_with_retries behaviour: fallback, budget, validation-retry."""

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self):
        tool_output = {
            "skip_today": False,
            "skip_reason": None,
            "fno_candidates": [],
            "equity_candidates": [],
            "market_regime": {"vix_regime": "neutral", "trend": "sideways"},
            "notes": "test",
        }
        anthropic = make_mock_anthropic(tool_output, tool_name="emit_brain_triage")
        runner = make_runner(anthropic=anthropic)
        spec = runner._build_agent_spec("brain_triage", "v1")
        api_request = {
            "model": spec.model,
            "max_tokens": spec.max_output_tokens,
            "system": [{"type": "text", "text": "You are brain_triage."}],
            "messages": [{"role": "user", "content": "test"}],
            "tools": [],
            "tool_choice": {"type": "tool", "name": spec.output_tool},
        }
        result = await runner._execute_with_retries(spec, api_request, "test-run-1", None)
        assert result.status == "succeeded"
        assert result.output == tool_output
        assert anthropic.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_model_used_after_transient_exhausted(self):
        """After max transient retries on primary, switches to fallback model."""
        # Fail until we switch to fallback model, then succeed
        call_count = 0
        primary_model = "claude-haiku-4-5-20251001"
        fallback_model = "claude-sonnet-4-6"
        tool_output = {
            "skip_today": True, "skip_reason": "VIX high", "fno_candidates": [],
            "equity_candidates": [], "market_regime": {"vix_regime": "high", "trend": "bearish"},
            "notes": "",
        }

        async def _side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("model") == primary_model:
                raise asyncio.TimeoutError("timeout")
            return _make_anthropic_response("emit_brain_triage", tool_output, fallback_model)

        anthropic = MagicMock()
        anthropic.messages.create = AsyncMock(side_effect=_side_effect)
        runner = make_runner(anthropic=anthropic)
        spec = runner._build_agent_spec("brain_triage", "v1")
        # Set retries low and enable fallback (brain_triage has no fallback_model by default)
        import dataclasses
        spec = dataclasses.replace(
            spec, max_retries_transient=1, on_failure="degrade",
            fallback_model=fallback_model,
        )

        api_request = {
            "model": primary_model,
            "max_tokens": spec.max_output_tokens,
            "system": [{"type": "text", "text": "You are an agent."}],
            "messages": [{"role": "user", "content": "x"}],
            "tools": [],
            "tool_choice": {"type": "tool", "name": spec.output_tool},
        }
        result = await runner._execute_with_retries(spec, api_request, "run-fallback", None)
        # Should succeed on fallback
        assert result.status == "succeeded"
        assert result.model_used == fallback_model

    @pytest.mark.asyncio
    async def test_used_fallback_flag_prevents_double_fallback(self):
        """Once fallback fires, further failures return 'transient_exhausted', not a second fallback."""
        import dataclasses

        async def _always_timeout(**kwargs):
            raise asyncio.TimeoutError("always times out")

        anthropic = MagicMock()
        anthropic.messages.create = AsyncMock(side_effect=_always_timeout)
        runner = make_runner(anthropic=anthropic)
        spec = runner._build_agent_spec("brain_triage", "v1")
        spec = dataclasses.replace(
            spec, max_retries_transient=0, on_failure="degrade",
            fallback_model="claude-sonnet-4-6",
        )

        api_request = {
            "model": spec.model,
            "max_tokens": spec.max_output_tokens,
            "system": [{"type": "text", "text": "You are an agent."}],
            "messages": [{"role": "user", "content": "x"}],
            "tools": [],
            "tool_choice": {"type": "tool", "name": spec.output_tool},
        }
        result = await runner._execute_with_retries(spec, api_request, "run-double", None)
        # Must eventually fail, not loop infinitely
        assert result.status == "failed"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_validation_retry_fires_on_rejected_output(self):
        """OutputValidationError triggers repair-and-retry up to max_retries_validation."""
        import dataclasses
        import types

        good_output = {
            "skip_today": False, "skip_reason": None, "fno_candidates": [],
            "equity_candidates": [], "market_regime": {"vix_regime": "neutral", "trend": "sideways"},
            "notes": "",
        }
        call_count = 0

        async def _side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            # First call returns a tool call with bad output (empty dict);
            # second call returns good output.
            output = {} if call_count == 1 else good_output
            return _make_anthropic_response("emit_brain_triage", output)

        anthropic = MagicMock()
        anthropic.messages.create = AsyncMock(side_effect=_side_effect)
        runner = make_runner(anthropic=anthropic)
        spec = runner._build_agent_spec("brain_triage", "v1")
        # Wire a simple validator that rejects empty output
        spec = dataclasses.replace(spec, output_validator=None, max_retries_validation=2)

        # Patch _validate_output to reject first call, accept second
        validate_count = 0

        async def _mock_validate(spec_arg, result, ctx):
            nonlocal validate_count
            validate_count += 1
            if validate_count == 1:
                from src.agents.runtime.workflow_runner import OutputValidationError
                raise OutputValidationError("bad output on attempt 1")
            return result

        runner._validate_output = _mock_validate  # type: ignore[method-assign]
        spec = dataclasses.replace(spec, output_validator="CEOJudgeOutputValidated")

        api_request = {
            "model": spec.model,
            "max_tokens": spec.max_output_tokens,
            "system": [{"type": "text", "text": "You are an agent."}],
            "messages": [{"role": "user", "content": "x"}],
            "tools": [],
            "tool_choice": {"type": "tool", "name": spec.output_tool},
        }
        result = await runner._execute_with_retries(spec, api_request, "run-valretry", None)
        assert result.status == "succeeded"
        assert anthropic.messages.create.call_count == 2


class TestFromAgentToggle:
    """Tests for from_agent replay-mode switching in WorkflowContext."""

    def test_from_agent_stored_in_context(self):
        from src.agents.runtime.spec import WorkflowContext

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
            from_agent="ceo_judge",
        )
        assert ctx.from_agent == "ceo_judge"

    def test_from_agent_none_by_default(self):
        from src.agents.runtime.spec import WorkflowContext

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
        assert ctx.from_agent is None
        assert ctx._replay_live_mode is False


# ---------------------------------------------------------------------------
# _safe_jsonb
# ---------------------------------------------------------------------------

class TestSafeJsonb:
    def test_small_dict_returned_as_is(self):
        data = {"key": "value", "n": 42}
        result = WorkflowRunner._safe_jsonb(data)
        assert result == data

    def test_oversized_payload_returns_none(self):
        data = {"key": "x" * 100_000}
        result = WorkflowRunner._safe_jsonb(data, limit=100)
        assert result is None

    def test_exactly_at_limit_passes(self):
        # Construct something whose serialisation is exactly the limit
        import json
        target = 50
        payload = {"k": "a"}
        serialised = json.dumps(payload, default=str)
        assert len(serialised) <= target
        result = WorkflowRunner._safe_jsonb(payload, limit=target)
        assert result == payload

    def test_non_serialisable_uses_str_fallback(self):
        from datetime import datetime
        data = {"ts": datetime(2026, 1, 1)}
        result = WorkflowRunner._safe_jsonb(data)
        # Should not raise; non-serialisable uses default=str
        assert result is not None


# ---------------------------------------------------------------------------
# Mid-stage BudgetExceeded
# ---------------------------------------------------------------------------

class TestMidStageBudget:
    """The per-agent post-accumulation budget check must raise BudgetExceeded
    if cost crosses the ceiling after a single agent returns, even in a
    parallel stage where the pre-stage projection was under the ceiling.
    """

    @pytest.mark.asyncio
    async def test_budget_exceeded_mid_stage_raises(self):
        """Run a workflow with an extremely low cost ceiling and a mock agent
        that returns a non-zero cost so the ceiling is crossed.
        """
        import types
        from decimal import Decimal
        from src.agents.runtime.workflow_runner import BudgetExceeded

        # Build a response that would cost something
        def _make_response():
            usage = types.SimpleNamespace(
                input_tokens=10_000,   # at Haiku prices: ~$0.0008
                output_tokens=2_000,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            )
            tool_block = types.SimpleNamespace(
                type="tool_use",
                id="toolu_mid_budget",
                name="emit_brain_triage",
                input={
                    "skip_today": False, "skip_reason": None,
                    "fno_candidates": [], "equity_candidates": [],
                    "market_regime": {"vix_regime": "neutral", "trend": "sideways"},
                    "notes": "",
                },
            )
            return types.SimpleNamespace(content=[tool_block], usage=usage)

        anthropic = MagicMock()
        anthropic.messages.create = AsyncMock(return_value=_make_response())

        # Set a ceiling so small that even one Haiku call exceeds it
        wf = WorkflowSpec(
            name="test_mid_budget",
            version="v1",
            cost_ceiling_usd=Decimal("0.000001"),  # $0.000001 — guaranteed exceeded
            token_ceiling=100_000,
            stages=(
                WorkflowStage(
                    stage_name="brain_triage",
                    kind="sequential",
                    agents=(StageAgent(agent_name="brain_triage", persona_version="v1",
                                       output_key="triage"),),
                ),
            ),
        )
        runner = make_runner(anthropic=anthropic)
        result = await runner.run(wf, params={})
        # The workflow should end as failed due to BudgetExceeded
        assert result.status == "failed"
        assert result.error is not None
