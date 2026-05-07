"""Sub-agent parallel fan-out helper for the Historical Explorer pod."""
from __future__ import annotations

import asyncio
import logging

from src.agents.runtime.spec import (
    AgentRunResult,
    StageAgent,
    WorkflowContext,
    _AgentInvocation,
)

log = logging.getLogger(__name__)


async def run_subagents_parallel(
    runner: "WorkflowRunner",  # type: ignore[name-defined]  # avoid circular import
    subagent_specs: list[StageAgent],
    shared_input: dict,
    ctx: WorkflowContext,
    aggregate_into: str,
) -> dict[str, dict | None]:
    """Run N agents in parallel against the same shared_input.

    Returns their outputs as {agent_name: output_or_None}. Used by the
    Historical Explorer pod (trend, past_predictions, sentiment_drift,
    fno_positioning) and any future multi-perspective fan-out.

    Failures respect each sub-agent's on_iteration_failure policy. If any
    agent's policy is 'abort_workflow', the exception propagates immediately.
    """
    coros = []
    for sa in subagent_specs:
        invocation = _AgentInvocation(stage_agent=sa, item=shared_input, item_index=0)
        coros.append(runner._invoke_agent(invocation, ctx))

    results: list[AgentRunResult | BaseException] = await asyncio.gather(
        *coros, return_exceptions=True
    )

    aggregated: dict[str, dict | None] = {}
    for sa, res in zip(subagent_specs, results):
        if isinstance(res, BaseException):
            if sa.on_iteration_failure == "abort_workflow":
                raise res
            log.error(f"Sub-agent {sa.agent_name} failed in fan-out: {res}")
            aggregated[sa.agent_name] = None
        else:
            ctx.agent_run_results.append(res)
            ctx.cost_so_far_usd += res.cost_usd
            ctx.tokens_so_far += res.input_tokens + res.output_tokens
            aggregated[sa.agent_name] = res.output

    ctx.stage_outputs[aggregate_into] = aggregated
    return aggregated
