"""Tests for src/eval/ab.py — A/B replay helpers."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.eval.ab import (
    _decisions_changed,
    _extract_expected_pnl,
    _extract_expected_pnl_from_result,
    _sample_diverse_runs,
)


# ---------------------------------------------------------------------------
# _sample_diverse_runs
# ---------------------------------------------------------------------------

class TestSampleDiverseRuns:
    def test_returns_all_when_under_limit(self):
        runs = [{"id": i} for i in range(3)]
        sampled = _sample_diverse_runs(runs, n=5)
        assert len(sampled) == 3

    def test_returns_n_when_over_limit(self):
        runs = [{"id": i} for i in range(20)]
        sampled = _sample_diverse_runs(runs, n=5)
        assert len(sampled) == 5

    def test_returns_list_not_same_object(self):
        runs = [{"id": 1}, {"id": 2}]
        sampled = _sample_diverse_runs(runs, n=5)
        # Must be a different list object (not the same reference)
        assert sampled is not runs


# ---------------------------------------------------------------------------
# _extract_expected_pnl
# ---------------------------------------------------------------------------

class TestExtractExpectedPnl:
    def test_reads_from_stage_outputs(self):
        run = {
            "stage_outputs": {"judge_verdict": {"expected_book_pnl_pct": 8.5}},
            "params": {},
        }
        assert _extract_expected_pnl(run) == 8.5

    def test_falls_back_to_params(self):
        run = {
            "stage_outputs": {},
            "params": {"expected_book_pnl_pct": 6.0},
        }
        assert _extract_expected_pnl(run) == 6.0

    def test_returns_none_when_missing(self):
        run = {"stage_outputs": {}, "params": {}}
        assert _extract_expected_pnl(run) is None


# ---------------------------------------------------------------------------
# _extract_expected_pnl_from_result
# ---------------------------------------------------------------------------

class TestExtractExpectedPnlFromResult:
    def test_reads_from_stage_outputs(self):
        result = MagicMock()
        result.stage_outputs = {"judge_verdict": {"expected_book_pnl_pct": 12.0}}
        result.predictions = []
        assert _extract_expected_pnl_from_result(result) == 12.0

    def test_falls_back_to_predictions(self):
        result = MagicMock()
        result.stage_outputs = {}
        result.predictions = [{"expected_pnl_pct": 5.0}]
        assert _extract_expected_pnl_from_result(result) == 5.0

    def test_returns_none_when_empty(self):
        result = MagicMock()
        result.stage_outputs = {}
        result.predictions = []
        assert _extract_expected_pnl_from_result(result) is None


# ---------------------------------------------------------------------------
# _decisions_changed
# ---------------------------------------------------------------------------

class TestDecisionsChanged:
    def test_same_decision_returns_false(self):
        original_run = {
            "stage_outputs": {"judge_verdict": {"decision_summary": "BUY BANKNIFTY long"}}
        }
        replay = MagicMock()
        replay.stage_outputs = {"judge_verdict": {"decision_summary": "BUY BANKNIFTY long"}}
        assert _decisions_changed(original_run, replay) is False

    def test_different_decision_returns_true(self):
        original_run = {
            "stage_outputs": {"judge_verdict": {"decision_summary": "BUY BANKNIFTY long"}}
        }
        replay = MagicMock()
        replay.stage_outputs = {"judge_verdict": {"decision_summary": "SKIP — VIX too high"}}
        assert _decisions_changed(original_run, replay) is True

    def test_both_empty_returns_false(self):
        original_run = {"stage_outputs": {}, "params": {}}
        replay = MagicMock()
        replay.stage_outputs = {}
        assert _decisions_changed(original_run, replay) is False

    def test_one_empty_one_non_empty_returns_true(self):
        original_run = {
            "stage_outputs": {"judge_verdict": {"decision_summary": "BUY NIFTY"}}
        }
        replay = MagicMock()
        replay.stage_outputs = {}
        assert _decisions_changed(original_run, replay) is True


# ---------------------------------------------------------------------------
# run_prompt_version_ab (integration-style, mocked replay)
# ---------------------------------------------------------------------------

class TestRunPromptVersionAb:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_workflow_runs(self):
        from src.eval.ab import run_prompt_version_ab
        from src.eval.weekly import WeekData
        from datetime import date

        week = WeekData(start=date(2026, 4, 28), end=date(2026, 5, 2))
        runner = MagicMock()
        results = await run_prompt_version_ab(runner, week, ["fno_expert=v2"], n_sample=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_aggregates_results_per_candidate(self):
        from src.eval.ab import run_prompt_version_ab
        from src.eval.weekly import WeekData
        from datetime import date

        week = WeekData(start=date(2026, 4, 28), end=date(2026, 5, 2))
        week.workflow_runs = [{"id": "run-1", "stage_outputs": {}, "params": {}}]

        replay_result = MagicMock()
        replay_result.workflow_run_id = "replay-1"
        replay_result.stage_outputs = {}
        replay_result.predictions = []

        with patch(
            "src.agents.runtime.replay.replay_workflow_run",
            new=AsyncMock(return_value=replay_result),
        ):
            results = await run_prompt_version_ab(
                MagicMock(), week, ["fno_expert=v2"], n_sample=5
            )

        assert len(results) == 1
        assert results[0]["agent"] == "fno_expert"
        assert results[0]["candidate_version"] == "v2"
        assert results[0]["n_replays"] == 1

    @pytest.mark.asyncio
    async def test_replay_failure_is_logged_not_raised(self):
        from src.eval.ab import run_prompt_version_ab
        from src.eval.weekly import WeekData
        from datetime import date

        week = WeekData(start=date(2026, 4, 28), end=date(2026, 5, 2))
        week.workflow_runs = [{"id": "run-1", "stage_outputs": {}, "params": {}}]

        with patch(
            "src.agents.runtime.replay.replay_workflow_run",
            new=AsyncMock(side_effect=RuntimeError("replay crashed")),
        ):
            # Should not raise
            results = await run_prompt_version_ab(
                MagicMock(), week, ["fno_expert=v2"], n_sample=5
            )

        # No results collected because all replays failed
        assert results == []
