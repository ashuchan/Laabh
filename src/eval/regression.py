"""Regression suite runner — re-runs known seeds to detect prompt drift."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SEEDS_PATH = Path(__file__).parent.parent.parent / "tests" / "eval" / "seeds" / "regression_suite.json"


def load_seeds(path: str | Path | None = None) -> list[dict]:
    """Load regression seeds from JSON file."""
    seeds_file = Path(path) if path else SEEDS_PATH
    if not seeds_file.exists():
        log.warning(f"Regression suite seeds not found at {seeds_file}")
        return []
    try:
        data = json.loads(seeds_file.read_text())
        return data.get("seeds", [])
    except Exception as e:
        log.error(f"Failed to load regression seeds: {e}")
        return []


def check_expected_outcomes(replay_run: Any, expected: dict) -> dict:
    """Check a replay run's outputs against expected_outcomes dict.

    Returns {"all_passed": bool, "failures": list[str]}.
    """
    failures: list[str] = []

    for key, expected_val in expected.items():
        try:
            _check_one(replay_run, key, expected_val, failures)
        except Exception as e:
            failures.append(f"Error checking {key!r}: {e}")

    return {"all_passed": len(failures) == 0, "failures": failures}


def _check_one(replay_run: Any, key: str, expected_val: Any, failures: list[str]) -> None:
    """Evaluate one expected_outcome key."""
    stage_outputs = getattr(replay_run, "stage_outputs", {})

    if key == "brain_triage_skip_today":
        triage = stage_outputs.get("triage") or {}
        actual = triage.get("skip_today", False)
        if actual != expected_val:
            failures.append(f"brain_triage.skip_today={actual!r}, expected {expected_val!r}")

    elif key == "skip_reason_contains":
        triage = stage_outputs.get("triage") or {}
        reason = triage.get("skip_reason") or ""
        for term in (expected_val if isinstance(expected_val, list) else [expected_val]):
            if term.lower() not in reason.lower():
                failures.append(f"skip_reason {reason!r} does not contain {term!r}")

    elif key == "ceo_judge_decision_summary_contains":
        verdict = stage_outputs.get("judge_verdict") or {}
        summary = verdict.get("decision_summary") or ""
        for term in (expected_val if isinstance(expected_val, list) else [expected_val]):
            if term.lower() not in summary.lower():
                failures.append(
                    f"judge_verdict.decision_summary does not contain {term!r}"
                )

    elif key == "expected_book_pnl_pct_min":
        verdict = stage_outputs.get("judge_verdict") or {}
        actual = verdict.get("expected_book_pnl_pct") or 0
        if actual < expected_val:
            failures.append(
                f"expected_book_pnl_pct={actual} < min {expected_val}"
            )

    elif key == "expected_book_pnl_pct_max":
        verdict = stage_outputs.get("judge_verdict") or {}
        actual = verdict.get("expected_book_pnl_pct") or 0
        if actual > expected_val:
            failures.append(
                f"expected_book_pnl_pct={actual} > max {expected_val}"
            )

    elif key == "explorer_aggregator_do_not_repeat_count_min":
        aggregates = stage_outputs.get("explorer_aggregates") or []
        if isinstance(aggregates, list):
            total_dnr = sum(len(a.get("do_not_repeat", [])) for a in aggregates if a)
        else:
            total_dnr = 0
        if total_dnr < expected_val:
            failures.append(
                f"Total do_not_repeat count={total_dnr} < min {expected_val}"
            )

    elif key == "ceo_judge_explicit_skips_contains":
        verdict = stage_outputs.get("judge_verdict") or {}
        # Check if the symbol appears in any kill_switch or allocation skip
        judge_text = json.dumps(verdict).lower()
        for sym in (expected_val if isinstance(expected_val, list) else [expected_val]):
            if sym.lower() not in judge_text:
                failures.append(f"CEO Judge output does not reference {sym!r}")

    elif key == "must_not_be_REFUSED":
        predictions = getattr(replay_run, "predictions", [])
        if expected_val and not predictions:
            failures.append("Expected at least one prediction (not refused), got none")

    elif key == "must_use_strategy":
        predictions = getattr(replay_run, "predictions", [])
        strategy_used = any(
            expected_val.lower() in str(p.get("decision", "")).lower()
            for p in predictions
        )
        if not strategy_used:
            failures.append(f"Strategy {expected_val!r} not found in predictions")


async def run_regression_suite(
    runner: Any,
    seeds_path: str | Path | None = None,
) -> list[dict]:
    """Re-run every seed via replay and check expected_outcomes.

    Uses faithful replay (no override) so cost is zero for unchanged agents.
    """
    from src.agents.runtime.replay import replay_workflow_run

    seeds = load_seeds(seeds_path)
    if not seeds:
        return []

    results: list[dict] = []
    for seed in seeds:
        if seed.get("placeholder_uuid"):
            log.info(
                f"Skipping seed {seed['id']!r} — placeholder UUID. "
                "Replace workflow_run_id with a real production UUID to activate."
            )
            results.append({
                "seed_id": seed["id"],
                "rationale": seed.get("rationale", ""),
                "tags": seed.get("tags", []),
                "passed": None,
                "failures": [],
                "skipped": True,
            })
            continue
        try:
            replay_run = await replay_workflow_run(
                runner=runner,
                original_workflow_run_id=seed["workflow_run_id"],
                persona_version_override=None,
            )
            outcome = check_expected_outcomes(replay_run, seed.get("expected_outcomes", {}))
            results.append({
                "seed_id": seed["id"],
                "rationale": seed.get("rationale", ""),
                "tags": seed.get("tags", []),
                "passed": outcome["all_passed"],
                "failures": outcome["failures"],
                "replay_run_id": replay_run.workflow_run_id,
            })
        except Exception as e:
            results.append({
                "seed_id": seed.get("id", "unknown"),
                "passed": False,
                "failures": [f"Replay crashed: {e}"],
                "tags": seed.get("tags", []),
            })

    n_passed = sum(1 for r in results if r["passed"])
    log.info(f"Regression suite: {n_passed}/{len(results)} seeds passed")
    return results
