"""Tests for src/eval/weekly.py — P&L attribution, calibration drift, cost, rendering."""
from __future__ import annotations

from datetime import date

import pytest

from src.eval.weekly import (
    WeekData,
    compute_calibration_drift,
    compute_cost_per_correct_prediction,
    compute_pnl_attribution,
    render_markdown_report,
)


def _make_week(resolved: list[dict] | None = None, workflow_runs: list[dict] | None = None,
               agent_runs: list[dict] | None = None) -> WeekData:
    week = WeekData(start=date(2026, 4, 28), end=date(2026, 5, 2))
    week.resolved_predictions = resolved or []
    week.workflow_runs = workflow_runs or []
    week.agent_runs = agent_runs or []
    week.all_predictions_count = len(resolved or [])
    return week


# ---------------------------------------------------------------------------
# compute_pnl_attribution
# ---------------------------------------------------------------------------

class TestComputePnlAttribution:
    def test_empty_predictions_returns_zero(self):
        week = _make_week()
        result = compute_pnl_attribution(week)
        assert result["week_total_pnl_pct"] == 0
        assert result["attribution"] == []

    def test_sums_pnl_correctly(self):
        week = _make_week(resolved=[
            {"realised_pnl_pct": 5.0, "conviction": 0.75},
            {"realised_pnl_pct": -2.0, "conviction": 0.65},
            {"realised_pnl_pct": 3.0, "conviction": 0.80},
        ])
        result = compute_pnl_attribution(week)
        assert abs(result["week_total_pnl_pct"] - 6.0) < 1e-6

    def test_win_rate_computed(self):
        week = _make_week(resolved=[
            {"realised_pnl_pct": 5.0},
            {"realised_pnl_pct": -2.0},
            {"realised_pnl_pct": 3.0},
            {"realised_pnl_pct": -1.0},
        ])
        result = compute_pnl_attribution(week)
        assert result["n_wins"] == 2
        assert result["win_rate_pct"] == 50.0

    def test_none_pnl_treated_as_zero(self):
        week = _make_week(resolved=[
            {"realised_pnl_pct": None},
            {"realised_pnl_pct": 4.0},
        ])
        result = compute_pnl_attribution(week)
        assert result["week_total_pnl_pct"] == 4.0


# ---------------------------------------------------------------------------
# compute_calibration_drift
# ---------------------------------------------------------------------------

class TestComputeCalibrationDrift:
    def test_empty_predictions_returns_empty_bins(self):
        week = _make_week()
        result = compute_calibration_drift(week)
        assert result["ceo_judge"] == []

    def test_bins_with_fewer_than_3_excluded(self):
        # Only 2 predictions in the 0.7-0.8 bin — should be excluded
        week = _make_week(resolved=[
            {"conviction": 0.72, "realised_pnl_pct": 5.0},
            {"conviction": 0.75, "realised_pnl_pct": -2.0},
        ])
        result = compute_calibration_drift(week)
        assert result["ceo_judge"] == []

    def test_well_calibrated_verdict(self):
        # 0.7-0.8 bin: expected ~0.75, set actual to 0.75 (3/4 wins)
        week = _make_week(resolved=[
            {"conviction": 0.72, "realised_pnl_pct": 5.0},
            {"conviction": 0.73, "realised_pnl_pct": 3.0},
            {"conviction": 0.74, "realised_pnl_pct": 2.0},
            {"conviction": 0.75, "realised_pnl_pct": -1.0},
        ])
        result = compute_calibration_drift(week)
        bins = result["ceo_judge"]
        assert len(bins) == 1
        row = bins[0]
        assert row["bin"] == "0.7-0.8"
        assert row["n"] == 4

    def test_overconfident_verdict(self):
        # All lose but conviction is 0.7-0.8 → actual < expected → overconfident
        week = _make_week(resolved=[
            {"conviction": 0.71, "realised_pnl_pct": -1.0},
            {"conviction": 0.72, "realised_pnl_pct": -2.0},
            {"conviction": 0.73, "realised_pnl_pct": -3.0},
        ])
        result = compute_calibration_drift(week)
        row = result["ceo_judge"][0]
        assert row["verdict"] == "overconfident"

    def test_underconfident_verdict(self):
        # All win but conviction is 0.5-0.6 → actual > expected → underconfident
        week = _make_week(resolved=[
            {"conviction": 0.51, "realised_pnl_pct": 5.0},
            {"conviction": 0.52, "realised_pnl_pct": 3.0},
            {"conviction": 0.53, "realised_pnl_pct": 2.0},
        ])
        result = compute_calibration_drift(week)
        row = result["ceo_judge"][0]
        assert row["verdict"] == "underconfident"


# ---------------------------------------------------------------------------
# compute_cost_per_correct_prediction
# ---------------------------------------------------------------------------

class TestComputeCostPerCorrectPrediction:
    def test_empty_returns_none_per_unit(self):
        week = _make_week()
        result = compute_cost_per_correct_prediction(week)
        assert result["cost_per_prediction_usd"] is None
        assert result["cost_per_win_usd"] is None

    def test_cost_per_prediction_computed(self):
        week = _make_week(
            resolved=[
                {"realised_pnl_pct": 5.0},
                {"realised_pnl_pct": -1.0},
            ],
            workflow_runs=[{"cost_usd": 0.50}],
        )
        result = compute_cost_per_correct_prediction(week)
        assert abs(result["cost_per_prediction_usd"] - 0.25) < 1e-6

    def test_cost_per_win_computed(self):
        week = _make_week(
            resolved=[
                {"realised_pnl_pct": 5.0},
                {"realised_pnl_pct": -1.0},
            ],
            workflow_runs=[{"cost_usd": 1.00}],
        )
        result = compute_cost_per_correct_prediction(week)
        assert abs(result["cost_per_win_usd"] - 1.00) < 1e-6  # 1 win

    def test_by_agent_breakdown(self):
        week = _make_week(
            workflow_runs=[{"cost_usd": 1.0}],
            agent_runs=[
                {"agent_name": "ceo_judge", "cost_usd": 0.30},
                {"agent_name": "ceo_judge", "cost_usd": 0.20},
                {"agent_name": "brain_triage", "cost_usd": 0.10},
            ],
        )
        result = compute_cost_per_correct_prediction(week)
        assert abs(result["by_agent"]["ceo_judge"] - 0.50) < 1e-6
        assert abs(result["by_agent"]["brain_triage"] - 0.10) < 1e-6


# ---------------------------------------------------------------------------
# render_markdown_report
# ---------------------------------------------------------------------------

class TestRenderMarkdownReport:
    def _make_report(self, regression_results=None, ab_results=None) -> str:
        week = _make_week(
            resolved=[{"realised_pnl_pct": 5.0}],
            workflow_runs=[{"cost_usd": 0.50}],
        )
        week.all_predictions_count = 3
        pnl = compute_pnl_attribution(week)
        cal = compute_calibration_drift(week)
        cost = compute_cost_per_correct_prediction(week)
        return render_markdown_report(
            week_iso="2026-W18",
            week=week,
            pnl_attribution=pnl,
            calibration_drift=cal,
            cost_correct=cost,
            regression_results=regression_results or [],
            ab_results=ab_results or [],
        )

    def test_report_contains_week_header(self):
        report = self._make_report()
        assert "2026-W18" in report

    def test_report_contains_pnl(self):
        report = self._make_report()
        assert "+5.0%" in report

    def test_skipped_seed_shows_placeholder_note(self):
        report = self._make_report(regression_results=[
            {"seed_id": "seed_001", "tags": ["high_conviction"], "skipped": True}
        ])
        assert "skipped" in report
        assert "placeholder UUID" in report

    def test_passed_seed_shows_passed(self):
        report = self._make_report(regression_results=[
            {"seed_id": "seed_001", "tags": [], "passed": True, "failures": []}
        ])
        assert "passed" in report

    def test_failed_seed_shows_degraded(self):
        report = self._make_report(regression_results=[
            {"seed_id": "seed_001", "tags": [], "passed": False,
             "failures": ["skip_reason missing VIX"]}
        ])
        assert "DEGRADED" in report
        assert "skip_reason missing VIX" in report

    def test_ab_section_present_when_ab_results(self):
        report = self._make_report(ab_results=[{
            "agent": "fno_expert",
            "candidate_version": "v2",
            "n_replays": 5,
            "n_decisions_changed": 2,
            "mean_expected_pnl_delta_pp": 1.5,
        }])
        assert "A/B Replay Results" in report
        assert "fno_expert=v2" in report

    def test_no_ab_section_when_empty(self):
        report = self._make_report(ab_results=[])
        assert "A/B Replay Results" not in report
