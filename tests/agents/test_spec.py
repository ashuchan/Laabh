"""Tests for AgentSpec, WorkflowSpec, and related dataclasses."""
import pytest
from decimal import Decimal

from src.agents.runtime.spec import (
    AgentSpec,
    AgentRunResult,
    StageAgent,
    WorkflowSpec,
    WorkflowStage,
    WorkflowContext,
    RunnerConfig,
    ToolContext,
)


class TestAgentSpec:
    def test_frozen(self):
        spec = AgentSpec(
            name="brain_triage",
            persona_version="v1",
            model="claude-haiku-4-5-20251001",
            fallback_model=None,
            tools=(),
            output_tool="emit_brain_triage",
            max_input_tokens=12_000,
            max_output_tokens=1_500,
        )
        with pytest.raises((AttributeError, TypeError)):
            spec.name = "other"  # type: ignore[misc]

    def test_tools_is_tuple(self):
        spec = AgentSpec(
            name="news_finder",
            persona_version="v1",
            model="claude-sonnet-4-6",
            fallback_model="claude-haiku-4-5-20251001",
            tools=("search_raw_content", "get_filings"),
            output_tool="emit_news_finder",
            max_input_tokens=16_000,
            max_output_tokens=2_500,
        )
        assert isinstance(spec.tools, tuple)
        assert "search_raw_content" in spec.tools

    def test_defaults(self):
        spec = AgentSpec(
            name="x",
            persona_version="v1",
            model="claude-sonnet-4-6",
            fallback_model=None,
            tools=(),
            output_tool=None,
            max_input_tokens=1000,
            max_output_tokens=100,
        )
        assert spec.temperature == 0.0
        assert spec.max_retries_transient == 3
        assert spec.max_retries_validation == 2
        assert spec.on_failure == "abort"
        assert spec.timeout_seconds == 60
        assert spec.cache_system is True
        assert spec.stream_response is False
        assert spec.cost_class == "medium"
        assert spec.output_validator is None


class TestWorkflowSpec:
    def test_frozen(self):
        ws = WorkflowSpec(
            name="test",
            version="v1",
            stages=(),
        )
        with pytest.raises((AttributeError, TypeError)):
            ws.name = "other"  # type: ignore[misc]

    def test_default_params(self):
        ws = WorkflowSpec(name="x", version="v1", stages=())
        assert isinstance(ws.default_params, dict)
        assert ws.cost_ceiling_usd == Decimal("5.0")
        assert ws.token_ceiling == 150_000
        assert ws.final_validators == ()


class TestAgentRunResult:
    def test_construction(self):
        result = AgentRunResult(
            agent_run_id="abc",
            agent_name="brain_triage",
            persona_version="v1",
            model_used="claude-haiku-4-5-20251001",
            status="succeeded",
            output={"skip_today": False},
            raw_output=None,
            cost_usd=Decimal("0.001"),
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            duration_ms=1500,
        )
        assert result.status == "succeeded"
        assert result.cost_usd == Decimal("0.001")
        assert result.validation_errors == []

    def test_failed_result(self):
        result = AgentRunResult(
            agent_run_id="xyz",
            agent_name="brain_triage",
            persona_version="v1",
            model_used="claude-haiku-4-5-20251001",
            status="failed",
            output=None,
            raw_output=None,
            cost_usd=Decimal("0"),
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            duration_ms=0,
            error="timeout",
        )
        assert result.status == "failed"
        assert result.error == "timeout"


class TestRunnerConfig:
    def test_defaults(self):
        cfg = RunnerConfig()
        assert cfg.default_temperature == 0.0
        assert cfg.enable_streaming is True
        assert cfg.enable_caching is True
        assert cfg.orphan_reconciliation_interval_minutes == 30

    def test_override(self):
        cfg = RunnerConfig(enable_streaming=False, telegram_chat_id="123")
        assert cfg.enable_streaming is False
        assert cfg.telegram_chat_id == "123"


class TestStageAgent:
    def test_defaults(self):
        sa = StageAgent(agent_name="brain_triage")
        assert sa.persona_version == "v1"
        assert sa.iteration_source is None
        assert sa.on_iteration_failure == "skip_one"
        assert sa.output_key == ""

    def test_custom(self):
        sa = StageAgent(
            agent_name="fno_expert",
            persona_version="v2",
            iteration_source="triage.fno_candidates",
            on_iteration_failure="abort_stage",
            output_key="fno_results",
        )
        assert sa.agent_name == "fno_expert"
        assert sa.iteration_source == "triage.fno_candidates"
