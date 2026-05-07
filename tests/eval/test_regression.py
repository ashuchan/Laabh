"""Tests for src/eval/regression.py — seed loading, outcome checking, suite runner."""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# load_seeds
# ---------------------------------------------------------------------------

class TestLoadSeeds:
    def test_loads_seeds_from_valid_file(self, tmp_path):
        from src.eval.regression import load_seeds

        seeds_file = tmp_path / "seeds.json"
        seeds_file.write_text(json.dumps({
            "version": "1.1",
            "seeds": [
                {"id": "seed_001", "workflow_run_id": "abc", "expected_outcomes": {}}
            ]
        }))
        seeds = load_seeds(seeds_file)
        assert len(seeds) == 1
        assert seeds[0]["id"] == "seed_001"

    def test_returns_empty_list_for_missing_file(self, tmp_path):
        from src.eval.regression import load_seeds

        seeds = load_seeds(tmp_path / "nonexistent.json")
        assert seeds == []

    def test_returns_empty_list_for_malformed_json(self, tmp_path):
        from src.eval.regression import load_seeds

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{broken json")
        seeds = load_seeds(bad_file)
        assert seeds == []

    def test_returns_empty_list_when_no_seeds_key(self, tmp_path):
        from src.eval.regression import load_seeds

        seeds_file = tmp_path / "seeds.json"
        seeds_file.write_text(json.dumps({"version": "1.0"}))
        seeds = load_seeds(seeds_file)
        assert seeds == []


# ---------------------------------------------------------------------------
# check_expected_outcomes
# ---------------------------------------------------------------------------

def _make_replay_run(stage_outputs=None, predictions=None):
    run = MagicMock()
    run.stage_outputs = stage_outputs or {}
    run.predictions = predictions or []
    return run


class TestCheckExpectedOutcomes:
    def test_empty_expected_always_passes(self):
        from src.eval.regression import check_expected_outcomes

        result = check_expected_outcomes(_make_replay_run(), {})
        assert result["all_passed"] is True
        assert result["failures"] == []

    def test_brain_triage_skip_today_true_passes(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={"triage": {"skip_today": True}})
        result = check_expected_outcomes(run, {"brain_triage_skip_today": True})
        assert result["all_passed"] is True

    def test_brain_triage_skip_today_mismatch_fails(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={"triage": {"skip_today": False}})
        result = check_expected_outcomes(run, {"brain_triage_skip_today": True})
        assert result["all_passed"] is False
        assert any("skip_today" in f for f in result["failures"])

    def test_skip_reason_contains_passes(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={
            "triage": {"skip_reason": "VIX is too high today"}
        })
        result = check_expected_outcomes(run, {"skip_reason_contains": ["VIX"]})
        assert result["all_passed"] is True

    def test_skip_reason_missing_term_fails(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={
            "triage": {"skip_reason": "market looks uncertain"}
        })
        result = check_expected_outcomes(run, {"skip_reason_contains": ["FOMC"]})
        assert result["all_passed"] is False

    def test_ceo_judge_decision_summary_contains(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={
            "judge_verdict": {"decision_summary": "BANKNIFTY long call spread deployed"}
        })
        result = check_expected_outcomes(run, {
            "ceo_judge_decision_summary_contains": ["BANKNIFTY", "long"]
        })
        assert result["all_passed"] is True

    def test_expected_book_pnl_pct_min_fails(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={
            "judge_verdict": {"expected_book_pnl_pct": 4.0}
        })
        result = check_expected_outcomes(run, {"expected_book_pnl_pct_min": 6})
        assert result["all_passed"] is False

    def test_expected_book_pnl_pct_max_fails(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={
            "judge_verdict": {"expected_book_pnl_pct": 25.0}
        })
        result = check_expected_outcomes(run, {"expected_book_pnl_pct_max": 20})
        assert result["all_passed"] is False

    def test_must_not_be_refused_fails_with_no_predictions(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(predictions=[])
        result = check_expected_outcomes(run, {"must_not_be_REFUSED": True})
        assert result["all_passed"] is False

    def test_must_not_be_refused_passes_with_predictions(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(predictions=[{"decision": "BUY_CALL_SPREAD"}])
        result = check_expected_outcomes(run, {"must_not_be_REFUSED": True})
        assert result["all_passed"] is True

    def test_must_use_strategy_passes(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(predictions=[{"decision": "BUY_CALL_SPREAD"}])
        result = check_expected_outcomes(run, {"must_use_strategy": "call_spread"})
        assert result["all_passed"] is True

    def test_must_use_strategy_fails(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(predictions=[{"decision": "HOLD"}])
        result = check_expected_outcomes(run, {"must_use_strategy": "iron_condor"})
        assert result["all_passed"] is False

    def test_explorer_aggregator_do_not_repeat_count_min(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run(stage_outputs={
            "explorer_aggregates": [
                {"do_not_repeat": ["TATAMOTORS", "RELIANCE"]},
                {"do_not_repeat": ["TCS"]},
            ]
        })
        result = check_expected_outcomes(run, {"explorer_aggregator_do_not_repeat_count_min": 2})
        assert result["all_passed"] is True

    def test_unknown_key_logs_error_not_crash(self):
        from src.eval.regression import check_expected_outcomes

        run = _make_replay_run()
        # Unknown keys should be captured as an error, not raise
        result = check_expected_outcomes(run, {"totally_unknown_key": "value"})
        # No exception raised; either passes or records an error gracefully
        assert isinstance(result["failures"], list)


# ---------------------------------------------------------------------------
# run_regression_suite
# ---------------------------------------------------------------------------

class TestRunRegressionSuite:
    @pytest.mark.asyncio
    async def test_placeholder_seeds_skipped(self, tmp_path):
        from src.eval.regression import run_regression_suite

        seeds_file = tmp_path / "seeds.json"
        seeds_file.write_text(json.dumps({
            "seeds": [{
                "id": "seed_001",
                "workflow_run_id": "00000000-0000-0000-0000-000000000001",
                "placeholder_uuid": True,
                "expected_outcomes": {},
                "tags": ["test"],
                "rationale": "placeholder",
            }]
        }))

        runner = MagicMock()
        results = await run_regression_suite(runner, seeds_path=seeds_file)
        assert len(results) == 1
        assert results[0]["skipped"] is True
        assert results[0]["seed_id"] == "seed_001"

    @pytest.mark.asyncio
    async def test_empty_seeds_returns_empty(self, tmp_path):
        from src.eval.regression import run_regression_suite

        seeds_file = tmp_path / "seeds.json"
        seeds_file.write_text(json.dumps({"seeds": []}))

        runner = MagicMock()
        results = await run_regression_suite(runner, seeds_path=seeds_file)
        assert results == []

    @pytest.mark.asyncio
    async def test_replay_crash_recorded_as_failure(self, tmp_path):
        from src.eval.regression import run_regression_suite

        seeds_file = tmp_path / "seeds.json"
        seeds_file.write_text(json.dumps({
            "seeds": [{
                "id": "seed_crash",
                "workflow_run_id": "real-uuid-here",
                "placeholder_uuid": False,
                "expected_outcomes": {},
                "tags": [],
                "rationale": "crash test",
            }]
        }))

        runner = MagicMock()
        with patch(
            "src.agents.runtime.replay.replay_workflow_run",
            new=AsyncMock(side_effect=RuntimeError("replay exploded"))
        ):
            results = await run_regression_suite(runner, seeds_path=seeds_file)

        assert len(results) == 1
        assert results[0]["passed"] is False
        assert "replay exploded" in results[0]["failures"][0]
