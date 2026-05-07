"""Replay a prior workflow_run (faithful or experimental A/B)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from src.agents.runtime.spec import WorkflowRunResult
    from src.agents.runtime.workflow_runner import WorkflowRunner

log = logging.getLogger(__name__)


async def replay_workflow_run(
    runner: "WorkflowRunner",
    original_workflow_run_id: str,
    from_agent: str | None = None,
    persona_version_override: dict[str, str] | None = None,
    experiment_tag: str | None = None,
) -> "WorkflowRunResult":
    """Re-execute a prior workflow_run.

    Faithful replay (no overrides): serves every agent's output from
    llm_audit_log — zero new API calls. Use to debug without re-spending.

    Experimental replay (with overrides): overridden agents make new API
    calls; others serve from cache. Only the overridden agents incur cost.

    Args:
        runner: a configured WorkflowRunner instance.
        original_workflow_run_id: UUID of the original workflow_run to replay.
        from_agent: if set, only agents AT OR AFTER this name make new calls;
            earlier agents serve from the audit log even if overridden.
        persona_version_override: e.g. {"fno_expert": "v2"} — swaps one agent's
            prompt version while keeping all others identical.
        experiment_tag: stored in workflow_runs.experiment_tag for grouping.

    Returns:
        WorkflowRunResult for the new (replay) run.
    """
    original = await _fetch_original_run(runner, original_workflow_run_id)
    if not original:
        raise ValueError(f"workflow_run {original_workflow_run_id!r} not found")

    from src.agents.workflows import WORKFLOW_REGISTRY
    workflow_spec = WORKFLOW_REGISTRY.get(original["workflow_name"])
    if not workflow_spec:
        raise ValueError(f"Workflow {original['workflow_name']!r} not in WORKFLOW_REGISTRY")

    params = dict(original.get("params") or {})
    params["original_workflow_run_id"] = original_workflow_run_id
    params["persona_version_overrides"] = persona_version_override or {}
    params["from_agent"] = from_agent

    triggered_by = "replay"
    idempotency_key = (
        f"replay-{original_workflow_run_id}-{from_agent or 'start'}"
        + (f"-{experiment_tag}" if experiment_tag else "")
    )

    result = await runner.run(
        workflow_spec=workflow_spec,
        params=params,
        triggered_by=triggered_by,
        idempotency_key=idempotency_key,
    )

    # Tag the new run with experiment_tag and parent linkage
    if experiment_tag:
        await _tag_replay_run(runner, result.workflow_run_id, original_workflow_run_id, experiment_tag)

    return result


async def _fetch_original_run(runner: "WorkflowRunner", run_id: str) -> dict | None:
    """Fetch original workflow_run metadata from the DB."""
    try:
        async with runner.db_session_factory() as db:
            result = await db.execute(
                text("""
                    SELECT workflow_name, version, params
                    FROM workflow_runs
                    WHERE id = :id
                """),
                {"id": run_id},
            )
            row = result.fetchone()
            if row:
                return {"workflow_name": row[0], "version": row[1], "params": row[2]}
    except Exception as e:
        log.warning(f"Failed to fetch original workflow_run {run_id!r}: {e}")
    return None


async def _tag_replay_run(
    runner: "WorkflowRunner",
    new_run_id: str,
    parent_run_id: str,
    experiment_tag: str,
) -> None:
    try:
        async with runner.db_session_factory() as db:
            await db.execute(
                text("""
                    UPDATE workflow_runs
                    SET parent_run_id = :parent,
                        experiment_tag = :tag
                    WHERE id = :id
                """),
                {"parent": parent_run_id, "tag": experiment_tag, "id": new_run_id},
            )
            await db.commit()
    except Exception as e:
        log.warning(f"Failed to tag replay run: {e}")
