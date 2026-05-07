"""Tests for health utilities: kill-switch, orphan reconciliation, cost projection."""
from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from src.agents.runtime.health import (
    check_kill_switch,
    project_workflow_cost,
)
from src.agents.runtime.spec import StageAgent, WorkflowSpec, WorkflowStage
from src.agents.personas import PERSONA_MANIFEST


class TestCheckKillSwitch:
    @pytest.mark.asyncio
    async def test_active_when_redis_returns_1(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=b"1")
        assert await check_kill_switch(redis) is True

    @pytest.mark.asyncio
    async def test_inactive_when_redis_returns_none(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        assert await check_kill_switch(redis) is False

    @pytest.mark.asyncio
    async def test_inactive_on_redis_error(self):
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=Exception("Redis down"))
        # Should not raise; treat as inactive
        result = await check_kill_switch(redis)
        assert result is False


class TestProjectWorkflowCost:
    def test_minimal_workflow_has_positive_cost(self):
        wf = WorkflowSpec(
            name="test",
            version="v1",
            stages=(
                WorkflowStage(
                    stage_name="brain_triage",
                    kind="sequential",
                    agents=(StageAgent(agent_name="brain_triage", persona_version="v1"),),
                ),
            ),
        )
        cost = project_workflow_cost(wf, PERSONA_MANIFEST)
        assert cost > Decimal("0")

    def test_iteration_source_multiplies_cost(self):
        wf_single = WorkflowSpec(
            name="test",
            version="v1",
            stages=(
                WorkflowStage(
                    stage_name="s",
                    kind="parallel",
                    agents=(StageAgent(agent_name="news_finder", persona_version="v1"),),
                ),
            ),
        )
        wf_iter = WorkflowSpec(
            name="test",
            version="v1",
            stages=(
                WorkflowStage(
                    stage_name="s",
                    kind="parallel",
                    agents=(StageAgent(
                        agent_name="news_finder",
                        persona_version="v1",
                        iteration_source="triage.fno_candidates",
                    ),),
                ),
            ),
        )
        single = project_workflow_cost(wf_single, PERSONA_MANIFEST)
        iterated = project_workflow_cost(wf_iter, PERSONA_MANIFEST)
        assert iterated == single * 5

    def test_unknown_agent_skipped_gracefully(self):
        wf = WorkflowSpec(
            name="test",
            version="v1",
            stages=(
                WorkflowStage(
                    stage_name="s",
                    kind="sequential",
                    agents=(StageAgent(agent_name="nonexistent", persona_version="v1"),),
                ),
            ),
        )
        cost = project_workflow_cost(wf, PERSONA_MANIFEST)
        assert cost == Decimal("0")

    def test_returns_decimal(self):
        wf = WorkflowSpec(
            name="test",
            version="v1",
            stages=(
                WorkflowStage(
                    stage_name="triage",
                    kind="sequential",
                    agents=(StageAgent(agent_name="brain_triage", persona_version="v1"),),
                ),
            ),
        )
        result = project_workflow_cost(wf, PERSONA_MANIFEST)
        assert isinstance(result, Decimal)
