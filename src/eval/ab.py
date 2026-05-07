"""Prompt-version A/B framework — experimental replay comparisons."""
from __future__ import annotations

import logging
import random
from typing import Any

log = logging.getLogger(__name__)


def _sample_diverse_runs(workflow_runs: list[dict], n: int = 5) -> list[dict]:
    """Sample up to n workflow_runs, preferring diversity over recency."""
    if len(workflow_runs) <= n:
        return workflow_runs[:]
    # Simple random sample — in production, prefer stratifying by regime
    return random.sample(workflow_runs, n)


async def run_prompt_version_ab(
    runner: Any,
    week_data: Any,
    candidate_versions: list[str],
    n_sample: int = 5,
) -> list[dict]:
    """Replay a sample of the week's runs with each candidate persona version.

    Args:
        runner: WorkflowRunner instance.
        week_data: WeekData from fetch_week_data.
        candidate_versions: list of "agent=version" strings, e.g. ["fno_expert=v2"].
        n_sample: number of original runs to replay per candidate.

    Returns:
        list of A/B result dicts.
    """
    from src.agents.runtime.replay import replay_workflow_run

    sample_runs = _sample_diverse_runs(week_data.workflow_runs, n_sample)
    ab_results: list[dict] = []

    for version_spec in candidate_versions:
        agent, version = version_spec.split("=", 1)
        version_runs: list[dict] = []

        for original_run in sample_runs:
            try:
                replay = await replay_workflow_run(
                    runner=runner,
                    original_workflow_run_id=str(original_run["id"]),
                    persona_version_override={agent: version},
                )
                original_pnl = _extract_expected_pnl(original_run)
                replay_pnl = _extract_expected_pnl_from_result(replay)
                version_runs.append({
                    "original_run_id": str(original_run["id"]),
                    "replay_run_id": replay.workflow_run_id,
                    "decision_changed": _decisions_changed(original_run, replay),
                    "expected_pnl_delta_pp": (replay_pnl - original_pnl)
                    if (original_pnl is not None and replay_pnl is not None)
                    else None,
                })
            except Exception as e:
                log.warning(f"A/B replay failed for {original_run['id']}: {e}")

        if version_runs:
            deltas = [r["expected_pnl_delta_pp"] for r in version_runs
                      if r["expected_pnl_delta_pp"] is not None]
            ab_results.append({
                "agent": agent,
                "candidate_version": version,
                "n_replays": len(version_runs),
                "n_decisions_changed": sum(1 for r in version_runs if r["decision_changed"]),
                "mean_expected_pnl_delta_pp": (sum(deltas) / len(deltas)) if deltas else None,
                "replays": version_runs,
            })

    return ab_results


def _extract_expected_pnl(workflow_run: dict) -> float | None:
    params = workflow_run.get("params") or {}
    return params.get("expected_book_pnl_pct")


def _extract_expected_pnl_from_result(result: Any) -> float | None:
    predictions = getattr(result, "predictions", [])
    if not predictions:
        return None
    return predictions[0].get("expected_pnl_pct")


def _decisions_changed(original_run: dict, replay_result: Any) -> bool:
    """Return True if the replay produced a different allocation than the original."""
    original_params = original_run.get("params") or {}
    original_decision = original_params.get("last_decision_summary", "")

    replay_predictions = getattr(replay_result, "predictions", [])
    if not replay_predictions:
        return False

    replay_decision = replay_predictions[0].get("decision", "")
    return original_decision != replay_decision
