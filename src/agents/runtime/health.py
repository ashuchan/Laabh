"""Operational health utilities: orphan reconciliation, kill-switch, cost projection."""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import text

from src.agents.runtime.pricing import project_agent_cost
from src.agents.runtime.spec import WorkflowSpec

log = logging.getLogger(__name__)


async def reconcile_orphan_runs(db_session_factory) -> int:
    """Mark workflow_runs with status='running' older than 1 hour as orphaned.

    Returns the count of runs marked.
    """
    async with db_session_factory() as db:
        result = await db.execute(
            text("""
                UPDATE workflow_runs
                SET status = 'failed',
                    status_extended = 'orphaned',
                    completed_at = NOW(),
                    error = 'Reconciled as orphan: no progress for >1h'
                WHERE status = 'running'
                  AND started_at < NOW() - INTERVAL '1 hour'
                RETURNING id
            """)
        )
        await db.commit()
        count = result.rowcount
    if count > 0:
        log.warning(f"Reconciled {count} orphaned workflow_run(s)")
    return count


async def check_kill_switch(redis) -> bool:
    """Return True if the operator kill-switch is active (Redis key laabh:kill_switch=1)."""
    try:
        value = await redis.get("laabh:kill_switch")
        return value == b"1"
    except Exception as e:
        log.warning(f"Kill-switch check failed (treating as inactive): {e}")
        return False


def project_workflow_cost(workflow_spec: WorkflowSpec, persona_manifest: dict) -> Decimal:
    """Worst-case USD cost projection for an entire workflow.

    Uses max_input_tokens × input_price + max_output_tokens × output_price per agent,
    with no cache discount (conservative). Multiply by iteration count where
    iteration_source is used — uses a default count of 5 for any fan-out agent.
    """
    total = Decimal("0")
    for stage in workflow_spec.stages:
        for sa in stage.agents:
            persona_def = _resolve_persona(persona_manifest, sa.agent_name, sa.persona_version)
            if persona_def is None:
                continue
            per_call = project_agent_cost(
                model=persona_def["model"],
                max_input_tokens=persona_def["max_input_tokens"],
                max_output_tokens=persona_def["max_output_tokens"],
            )
            n = 5 if sa.iteration_source else 1   # conservative fan-out estimate
            total += per_call * n
    return total


def _resolve_persona(manifest: dict, agent_name: str, version: str) -> dict | None:
    agent_versions = manifest.get(agent_name)
    if not agent_versions:
        return None
    return agent_versions.get(version)
