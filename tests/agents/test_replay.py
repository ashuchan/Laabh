"""Tests for replay_workflow_run."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_runner(db_rows=None, run_result=None):
    """Build a minimal mock WorkflowRunner."""
    runner = MagicMock()

    # db returns the given row from fetchone()
    row = db_rows  # dict or None

    async def _db_ctx():
        db = AsyncMock()
        if row:
            result = MagicMock()
            result.fetchone.return_value = (
                row.get("workflow_name", "predict_today_combined"),
                row.get("version", "v1"),
                row.get("params", {}),
            )
            db.execute = AsyncMock(return_value=result)
        else:
            result = MagicMock()
            result.fetchone.return_value = None
            db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        return db

    class _FakeCtxMgr:
        async def __aenter__(self_inner):
            return await _db_ctx()
        async def __aexit__(self_inner, *_):
            pass

    runner.db_session_factory = MagicMock(return_value=_FakeCtxMgr())

    # default run result
    default_result = MagicMock()
    default_result.workflow_run_id = "new-run-id-abc"
    runner.run = AsyncMock(return_value=run_result or default_result)

    return runner


# ---------------------------------------------------------------------------
# _fetch_original_run
# ---------------------------------------------------------------------------

class TestFetchOriginalRun:
    @pytest.mark.asyncio
    async def test_returns_dict_when_found(self):
        from src.agents.runtime.replay import _fetch_original_run

        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "version": "v1",
            "params": {"foo": "bar"},
        })
        result = await _fetch_original_run(runner, "some-uuid")
        assert result is not None
        assert result["workflow_name"] == "predict_today_combined"
        assert result["params"] == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        from src.agents.runtime.replay import _fetch_original_run

        runner = _make_runner(db_rows=None)
        result = await _fetch_original_run(runner, "missing-uuid")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_db_exception(self):
        from src.agents.runtime.replay import _fetch_original_run

        runner = MagicMock()

        class _FailCtx:
            async def __aenter__(self):
                raise RuntimeError("DB down")
            async def __aexit__(self, *_):
                pass

        runner.db_session_factory = MagicMock(return_value=_FailCtx())

        result = await _fetch_original_run(runner, "uuid")
        assert result is None


# ---------------------------------------------------------------------------
# replay_workflow_run — top-level
# ---------------------------------------------------------------------------

class TestReplayWorkflowRun:
    @pytest.mark.asyncio
    async def test_raises_when_original_not_found(self):
        from src.agents.runtime.replay import replay_workflow_run

        runner = _make_runner(db_rows=None)
        with pytest.raises(ValueError, match="not found"):
            await replay_workflow_run(runner, "00000000-0000-0000-0000-000000000099")

    @pytest.mark.asyncio
    async def test_raises_on_unknown_workflow_name(self):
        from src.agents.runtime.replay import replay_workflow_run

        runner = _make_runner(db_rows={
            "workflow_name": "nonexistent_workflow",
            "params": {},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY", {}):
            with pytest.raises(ValueError, match="not in WORKFLOW_REGISTRY"):
                await replay_workflow_run(runner, "some-uuid")

    @pytest.mark.asyncio
    async def test_faithful_replay_passes_correct_params(self):
        """Faithful replay (no overrides) sets persona_version_overrides={}."""
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "params": {"date": "2026-05-07"},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            result = await replay_workflow_run(
                runner,
                "orig-uuid-001",
                persona_version_override=None,
            )

        call_kwargs = runner.run.call_args
        params_sent = call_kwargs.kwargs["params"]
        assert params_sent["original_workflow_run_id"] == "orig-uuid-001"
        assert params_sent["persona_version_overrides"] == {}
        assert params_sent["from_agent"] is None

    @pytest.mark.asyncio
    async def test_experimental_replay_sets_overrides(self):
        """Experimental replay propagates persona_version_override correctly."""
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "params": {},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            await replay_workflow_run(
                runner,
                "orig-uuid-002",
                persona_version_override={"fno_expert": "v2"},
                experiment_tag="fno_v2_ab",
            )

        params_sent = runner.run.call_args.kwargs["params"]
        assert params_sent["persona_version_overrides"] == {"fno_expert": "v2"}

    @pytest.mark.asyncio
    async def test_from_agent_propagated(self):
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "params": {},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            await replay_workflow_run(
                runner,
                "orig-uuid-003",
                from_agent="ceo_judge",
            )

        params_sent = runner.run.call_args.kwargs["params"]
        assert params_sent["from_agent"] == "ceo_judge"

    @pytest.mark.asyncio
    async def test_idempotency_key_includes_run_id(self):
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "params": {},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            await replay_workflow_run(runner, "orig-uuid-004")

        idem_key = runner.run.call_args.kwargs["idempotency_key"]
        assert "orig-uuid-004" in idem_key

    @pytest.mark.asyncio
    async def test_triggered_by_is_replay(self):
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "params": {},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            await replay_workflow_run(runner, "orig-uuid-005")

        triggered_by = runner.run.call_args.kwargs["triggered_by"]
        assert triggered_by == "replay"

    @pytest.mark.asyncio
    async def test_returns_run_result(self):
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        expected = MagicMock()
        expected.workflow_run_id = "new-run-999"
        runner = _make_runner(
            db_rows={"workflow_name": "predict_today_combined", "params": {}},
            run_result=expected,
        )

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            result = await replay_workflow_run(runner, "orig-uuid-006")

        assert result.workflow_run_id == "new-run-999"

    @pytest.mark.asyncio
    async def test_experiment_tag_triggers_db_update(self):
        """When experiment_tag is set, _tag_replay_run should be called."""
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "params": {},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            with patch("src.agents.runtime.replay._tag_replay_run",
                       new_callable=AsyncMock) as mock_tag:
                await replay_workflow_run(
                    runner,
                    "orig-uuid-007",
                    experiment_tag="my_experiment",
                )

        mock_tag.assert_awaited_once()
        args = mock_tag.call_args[0]
        assert args[2] == "orig-uuid-007"
        assert args[3] == "my_experiment"

    @pytest.mark.asyncio
    async def test_no_experiment_tag_skips_db_update(self):
        """Without experiment_tag, _tag_replay_run must NOT be called."""
        from src.agents.runtime.replay import replay_workflow_run

        fake_spec = MagicMock()
        runner = _make_runner(db_rows={
            "workflow_name": "predict_today_combined",
            "params": {},
        })

        with patch("src.agents.workflows.WORKFLOW_REGISTRY",
                   {"predict_today_combined": fake_spec}):
            with patch("src.agents.runtime.replay._tag_replay_run",
                       new_callable=AsyncMock) as mock_tag:
                await replay_workflow_run(runner, "orig-uuid-008")

        mock_tag.assert_not_awaited()


# ---------------------------------------------------------------------------
# _tag_replay_run
# ---------------------------------------------------------------------------

class TestTagReplayRun:
    @pytest.mark.asyncio
    async def test_tag_silently_handles_db_error(self):
        from src.agents.runtime.replay import _tag_replay_run

        runner = MagicMock()

        class _FailCtx:
            async def __aenter__(self):
                raise RuntimeError("timeout")
            async def __aexit__(self, *_):
                pass

        runner.db_session_factory = MagicMock(return_value=_FailCtx())

        # Should not raise
        await _tag_replay_run(runner, "new-id", "parent-id", "tag")
