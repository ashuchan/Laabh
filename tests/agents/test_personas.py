"""Tests for the PERSONA_MANIFEST and OUTPUT_TOOL_SCHEMAS."""
import pytest

from src.agents.personas import PERSONA_MANIFEST, OUTPUT_TOOL_SCHEMAS


EXPECTED_AGENTS = [
    "brain_triage", "news_finder", "news_editor",
    "explorer_trend", "explorer_past_predictions", "explorer_sentiment_drift",
    "explorer_fno_positioning", "explorer_aggregator",
    "fno_expert", "equity_expert",
    "ceo_bull", "ceo_bear", "ceo_judge",
    "shadow_evaluator",
]

REQUIRED_PERSONA_FIELDS = [
    "model", "fallback_model", "tools", "output_tool",
    "max_input_tokens", "max_output_tokens", "temperature", "system_prompt",
]

EXPECTED_OUTPUT_TOOLS = [
    "emit_brain_triage", "emit_news_finder", "emit_news_editor",
    "emit_explorer_trend", "emit_explorer_past_predictions", "emit_explorer_sentiment_drift",
    "emit_explorer_fno_positioning", "emit_explorer_aggregator",
    "emit_fno_expert", "emit_equity_expert",
    "emit_ceo_bull", "emit_ceo_bear", "emit_ceo_judge",
    "emit_shadow_evaluator",
]


class TestPersonaManifest:
    @pytest.mark.parametrize("agent_name", EXPECTED_AGENTS)
    def test_agent_in_manifest(self, agent_name):
        assert agent_name in PERSONA_MANIFEST, f"{agent_name!r} not in PERSONA_MANIFEST"

    @pytest.mark.parametrize("agent_name", EXPECTED_AGENTS)
    def test_v1_version_present(self, agent_name):
        versions = PERSONA_MANIFEST[agent_name]
        assert "v1" in versions, f"{agent_name!r} has no v1 version"

    @pytest.mark.parametrize("agent_name", EXPECTED_AGENTS)
    def test_required_fields_present(self, agent_name):
        persona_def = PERSONA_MANIFEST[agent_name]["v1"]
        for field in REQUIRED_PERSONA_FIELDS:
            assert field in persona_def, f"{agent_name}.v1 missing field {field!r}"

    @pytest.mark.parametrize("agent_name", EXPECTED_AGENTS)
    def test_token_budgets_reasonable(self, agent_name):
        persona_def = PERSONA_MANIFEST[agent_name]["v1"]
        assert persona_def["max_input_tokens"] >= 1_000, f"{agent_name} input budget too small"
        assert persona_def["max_output_tokens"] >= 100, f"{agent_name} output budget too small"
        assert persona_def["max_input_tokens"] <= 30_000, f"{agent_name} input budget too large"

    @pytest.mark.parametrize("agent_name", EXPECTED_AGENTS)
    def test_model_is_known(self, agent_name):
        persona_def = PERSONA_MANIFEST[agent_name]["v1"]
        known_models = {
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
        }
        assert persona_def["model"] in known_models, (
            f"{agent_name}.model={persona_def['model']!r} not in known models"
        )

    @pytest.mark.parametrize("agent_name", EXPECTED_AGENTS)
    def test_system_prompt_not_empty(self, agent_name):
        persona_def = PERSONA_MANIFEST[agent_name]["v1"]
        assert len(persona_def["system_prompt"]) > 100, (
            f"{agent_name} system_prompt is too short (< 100 chars)"
        )

    def test_opus_agents_are_ceo(self):
        """Only CEO agents should use Opus — it's expensive."""
        opus_agents = [
            name for name, versions in PERSONA_MANIFEST.items()
            if versions.get("v1", {}).get("model") == "claude-opus-4-7"
        ]
        for name in opus_agents:
            assert "ceo" in name, f"Non-CEO agent {name!r} is using Opus — is this intentional?"

    def test_brain_triage_uses_haiku(self):
        assert PERSONA_MANIFEST["brain_triage"]["v1"]["model"] == "claude-haiku-4-5-20251001"

    def test_shadow_evaluator_has_no_data_tools(self):
        """Shadow evaluator should have no data-fetching tools (receives full context directly)."""
        tools = PERSONA_MANIFEST["shadow_evaluator"]["v1"]["tools"]
        assert len(tools) == 0


class TestOutputToolSchemas:
    @pytest.mark.parametrize("tool_name", EXPECTED_OUTPUT_TOOLS)
    def test_tool_in_schema_registry(self, tool_name):
        assert tool_name in OUTPUT_TOOL_SCHEMAS

    @pytest.mark.parametrize("tool_name", EXPECTED_OUTPUT_TOOLS)
    def test_tool_has_required_shape(self, tool_name):
        schema = OUTPUT_TOOL_SCHEMAS[tool_name]
        assert "name" in schema
        assert "description" in schema
        assert "input_schema" in schema
        assert schema["name"] == tool_name

    @pytest.mark.parametrize("tool_name", EXPECTED_OUTPUT_TOOLS)
    def test_input_schema_is_object(self, tool_name):
        schema = OUTPUT_TOOL_SCHEMAS[tool_name]
        assert schema["input_schema"]["type"] == "object"
        assert "properties" in schema["input_schema"]

    def test_brain_triage_tool_has_skip_today(self):
        schema = OUTPUT_TOOL_SCHEMAS["emit_brain_triage"]
        props = schema["input_schema"]["properties"]
        assert "skip_today" in props
        assert props["skip_today"]["type"] == "boolean"

    def test_ceo_judge_tool_has_kill_switches(self):
        schema = OUTPUT_TOOL_SCHEMAS["emit_ceo_judge"]
        props = schema["input_schema"]["properties"]
        assert "kill_switches" in props
        assert "allocation" in props

    def test_shadow_evaluator_has_scores(self):
        schema = OUTPUT_TOOL_SCHEMAS["emit_shadow_evaluator"]
        props = schema["input_schema"]["properties"]
        assert "scores" in props
        assert "alert_operator" in props
