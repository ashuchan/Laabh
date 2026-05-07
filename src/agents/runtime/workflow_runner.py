"""WorkflowRunner — single entry point for executing agentic workflows."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from src.agents.runtime.pricing import MODEL_PRICES, compute_cost, project_agent_cost
from src.agents.runtime.spec import (
    _AgentInvocation,
    AgentRunResult,
    AgentSpec,
    RunnerConfig,
    StageAgent,
    WorkflowContext,
    WorkflowRunResult,
    WorkflowSpec,
    WorkflowStage,
)

try:
    from anthropic import AsyncAnthropic, APIError, APIStatusError
    from anthropic import APIConnectionError
except ImportError:
    AsyncAnthropic = None  # type: ignore[assignment,misc]
    APIError = Exception  # type: ignore[assignment,misc]
    APIStatusError = Exception  # type: ignore[assignment,misc]
    APIConnectionError = Exception  # type: ignore[assignment,misc]

try:
    from pydantic import ValidationError
except ImportError:
    ValidationError = Exception  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

IST_OFFSET = 19800  # seconds (UTC+5:30)


class BudgetExceeded(RuntimeError):
    """Raised when cost or token ceiling is breached."""


class DuplicateRun(RuntimeError):
    """Raised when an idempotency_key collision is detected."""


class KillSwitchActivated(RuntimeError):
    """Raised when the operator kill-switch is active."""


class StageAborted(RuntimeError):
    """Raised when a stage-level failure propagates."""

    def __init__(self, stage_name: str, cause: Exception) -> None:
        super().__init__(f"Stage {stage_name!r} aborted: {cause}")
        self.cause = cause


class OutputValidationError(ValueError):
    """Raised when the model's output fails semantic validation."""


class WorkflowRunner:
    """Single entry point for executing one WorkflowRun.

    Stateless across runs — instantiate once, call run() per workflow.
    Concurrent runs are safe; each gets its own WorkflowContext.
    """

    def __init__(
        self,
        db_session_factory,
        redis,
        anthropic: "AsyncAnthropic | None" = None,
        telegram=None,
        config: RunnerConfig | None = None,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.redis = redis
        self.anthropic = anthropic
        self.telegram = telegram
        self.config = config or RunnerConfig()

        # Imported lazily to avoid circular imports at module level
        from src.agents.personas import PERSONA_MANIFEST
        from src.agents.tools.registry import TOOL_REGISTRY
        from src.agents.validators import VALIDATOR_REGISTRY

        self._persona_manifest = PERSONA_MANIFEST
        self._tool_registry = TOOL_REGISTRY
        self._validator_registry = VALIDATOR_REGISTRY

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(
        self,
        workflow_spec: WorkflowSpec,
        params: dict | None = None,
        triggered_by: str = "scheduled",
        idempotency_key: str | None = None,
    ) -> WorkflowRunResult:
        """Execute one workflow.

        Returns WorkflowRunResult with status, predictions, cost summary, and
        validator outcomes. Raises BudgetExceeded or DuplicateRun on hard errors.
        """
        # 1. Idempotency guard — check DB first (permanent), then Redis (fast path)
        if idempotency_key and await self._idempotency_taken(idempotency_key):
            raise DuplicateRun(f"Idempotency key already used: {idempotency_key!r}")

        # 2. Merge params
        merged_params: dict[str, Any] = {**workflow_spec.default_params, **(params or {})}

        # 3. Create workflow_run row
        workflow_run_id = str(uuid4())
        async with self.db_session_factory() as db:
            await self._create_workflow_run_row(
                db, workflow_run_id, workflow_spec, merged_params, triggered_by,
                idempotency_key=idempotency_key,
            )
            await db.commit()

        # 4. Build WorkflowContext
        as_of = merged_params.get("as_of") or datetime.now(timezone.utc)
        ctx = WorkflowContext(
            workflow_run_id=workflow_run_id,
            workflow_spec=workflow_spec,
            params=merged_params,
            cost_so_far_usd=Decimal("0"),
            tokens_so_far=0,
            agent_run_results=[],
            stage_outputs={},
            db_session_factory=self.db_session_factory,
            redis=self.redis,
            anthropic=self.anthropic,
            as_of=as_of,
            telegram=self.telegram,
            is_replay=triggered_by == "replay",
            persona_version_overrides=merged_params.get("persona_version_overrides", {}),
            from_agent=merged_params.get("from_agent"),
        )

        # 5. Execute stages
        try:
            for stage in workflow_spec.stages:
                # Kill-switch check
                if self.redis and await self._check_kill_switch():
                    raise KillSwitchActivated("Operator kill-switch active")

                # Cost circuit breaker — pre-stage projection
                projected = self._project_stage_cost(stage, ctx)
                if ctx.cost_so_far_usd + projected >= workflow_spec.cost_ceiling_usd:
                    await self._alert_telegram(
                        f"⚠️ Workflow {workflow_spec.name} pre-flight cost "
                        f"${ctx.cost_so_far_usd + projected:.3f} would exceed "
                        f"ceiling ${workflow_spec.cost_ceiling_usd}"
                    )
                    raise BudgetExceeded(
                        f"Projected cost ${ctx.cost_so_far_usd + projected:.3f} "
                        f">= ceiling ${workflow_spec.cost_ceiling_usd}"
                    )
                if ctx.tokens_so_far >= workflow_spec.token_ceiling:
                    raise BudgetExceeded(
                        f"Token ceiling {workflow_spec.token_ceiling} reached"
                    )

                await self._run_stage(stage, ctx)

                # Early exit: brain triage skip_today
                if self._should_short_circuit(stage, ctx):
                    return await self._finalize_workflow_run(
                        ctx,
                        status="succeeded",
                        short_circuit_reason="brain_skip_today",
                    )

            # 6. Final cross-agent validators
            validator_outcomes = await self._run_final_validators(workflow_spec, ctx)

            # 7. Persist agent_predictions
            predictions = await self._persist_predictions(ctx, validator_outcomes)

            # 8. Finalize status
            final_status = (
                "succeeded_with_caveats"
                if any(v["outcome"] in ("caveat", "rejected") for v in validator_outcomes)
                else "succeeded"
            )
            if final_status == "succeeded_with_caveats":
                await self._alert_telegram(
                    f"⚠️ Workflow {workflow_spec.name} succeeded with guardrail caveats",
                    severity="warning",
                )
            return await self._finalize_workflow_run(
                ctx,
                status="succeeded",
                status_extended=final_status,
                predictions=predictions,
                validator_outcomes=validator_outcomes,
            )

        except BudgetExceeded as e:
            await self._alert_telegram(f"❌ Workflow {workflow_spec.name} aborted: {e}")
            return await self._finalize_workflow_run(ctx, status="failed", error=str(e))
        except KillSwitchActivated as e:
            await self._alert_telegram(f"🛑 Workflow {workflow_spec.name} kill-switch: {e}")
            return await self._finalize_workflow_run(ctx, status="cancelled", error=str(e))
        except Exception as e:
            log.exception("Workflow run failed unexpectedly")
            await self._alert_telegram(f"❌ Workflow {workflow_spec.name} crashed: {e}")
            return await self._finalize_workflow_run(ctx, status="failed", error=str(e))

    # ------------------------------------------------------------------
    # Stage execution
    # ------------------------------------------------------------------

    async def _run_stage(self, stage: WorkflowStage, ctx: WorkflowContext) -> None:
        """Execute one stage; updates ctx.agent_run_results and ctx.stage_outputs."""
        if stage.kind == "conditional":
            if not self._evaluate_condition(stage.condition, ctx):
                log.info(f"Skipping conditional stage {stage.stage_name!r}")
                return

        invocations: list[_AgentInvocation] = []
        for sa in stage.agents:
            if sa.iteration_source:
                items = self._resolve_iteration(sa.iteration_source, ctx)
                for idx, item in enumerate(items):
                    invocations.append(_AgentInvocation(stage_agent=sa, item=item, item_index=idx))
            else:
                invocations.append(_AgentInvocation(stage_agent=sa, item=None, item_index=0))

        if stage.kind == "parallel":
            results: list[Any] = await asyncio.gather(
                *[self._invoke_agent(inv, ctx) for inv in invocations],
                return_exceptions=True,
            )
        else:  # sequential or conditional (already checked condition above)
            results = []
            for inv in invocations:
                results.append(await self._invoke_agent(inv, ctx))

        for inv, res in zip(invocations, results):
            if isinstance(res, BaseException):
                log.error(f"Agent {inv.stage_agent.agent_name} raised: {res}")
                policy = inv.stage_agent.on_iteration_failure
                if policy == "abort_workflow":
                    raise res  # type: ignore[misc]
                if policy == "abort_stage":
                    raise StageAborted(stage.stage_name, res)  # type: ignore[arg-type]
                # skip_one: continue
                continue

            ctx.agent_run_results.append(res)
            ctx.cost_so_far_usd += res.cost_usd
            ctx.tokens_so_far += res.input_tokens + res.output_tokens

            # Per-agent post-accumulation budget check (parallel stages can overshoot
            # a pre-stage projection; catch the breach as early as possible).
            if ctx.cost_so_far_usd >= ctx.workflow_spec.cost_ceiling_usd:
                raise BudgetExceeded(
                    f"Cost ceiling ${ctx.workflow_spec.cost_ceiling_usd} reached "
                    f"after agent {inv.stage_agent.agent_name} "
                    f"(actual ${ctx.cost_so_far_usd:.4f})"
                )
            if ctx.tokens_so_far >= ctx.workflow_spec.token_ceiling:
                raise BudgetExceeded(
                    f"Token ceiling {ctx.workflow_spec.token_ceiling} reached "
                    f"after agent {inv.stage_agent.agent_name}"
                )

            output_key = inv.stage_agent.output_key or inv.stage_agent.agent_name
            if inv.stage_agent.iteration_source:
                ctx.stage_outputs.setdefault(output_key, []).append(res.output)
            else:
                ctx.stage_outputs[output_key] = res.output

    # ------------------------------------------------------------------
    # Per-call lifecycle
    # ------------------------------------------------------------------

    async def _invoke_agent(
        self, invocation: _AgentInvocation, ctx: WorkflowContext
    ) -> AgentRunResult:
        """Full lifecycle for one agent call."""
        sa = invocation.stage_agent
        agent_run_id = str(uuid4())

        # Handle the _explorer_pod pseudo-agent
        if sa.agent_name == "_explorer_pod":
            return await self._run_explorer_pod(invocation, ctx, agent_run_id)

        spec = self._build_agent_spec(sa.agent_name, sa.persona_version)

        # Replay: serve from audit log if not overridden
        if ctx.is_replay and not self._is_overridden(spec, ctx):
            cached = await self._fetch_from_audit_log(spec, ctx, invocation)
            if cached:
                return self._materialize_result_from_audit(cached, spec, agent_run_id)

        # Assemble prompt
        prompt_messages = self._assemble_prompt(spec, invocation, ctx)
        input_tokens_estimate = self._estimate_input_tokens(spec, prompt_messages)

        await self._persist_agent_run_started(
            agent_run_id, sa, ctx, input_tokens_estimate, item_index=invocation.item_index
        )

        if input_tokens_estimate > spec.max_input_tokens:
            failed = self._make_failed_result(
                spec, agent_run_id, spec.model,
                f"Input estimate {input_tokens_estimate} exceeds max {spec.max_input_tokens}",
                "budget_exceeded",
            )
            await self._persist_agent_run_completed(failed, ctx)
            return failed

        api_request = self._build_api_request(spec, prompt_messages, ctx)

        result = await self._execute_with_retries(spec, api_request, agent_run_id, ctx)

        if result.status == "succeeded" and spec.name == "shadow_evaluator":
            try:
                from src.eval.shadow import persist_shadow_eval_output
                await persist_shadow_eval_output(result, ctx)
            except Exception as e:
                log.warning(f"shadow eval persist failed (non-fatal): {e}")

        await self._persist_agent_run_completed(result, ctx)
        return result

    async def _run_explorer_pod(
        self,
        invocation: _AgentInvocation,
        ctx: WorkflowContext,
        agent_run_id: str,
    ) -> AgentRunResult:
        """Expand the _explorer_pod pseudo-agent into 4 sub-agents + aggregator."""
        from src.agents.runtime.parallel import run_subagents_parallel

        candidate = invocation.item or {}
        is_fno = candidate.get("is_fno", candidate.get("underlying_id") is not None)

        subagent_specs = [
            StageAgent(agent_name="explorer_trend", persona_version="v1",
                       on_iteration_failure="skip_one"),
            StageAgent(agent_name="explorer_past_predictions", persona_version="v1",
                       on_iteration_failure="skip_one"),
            StageAgent(agent_name="explorer_sentiment_drift", persona_version="v1",
                       on_iteration_failure="skip_one"),
        ]
        if is_fno:
            subagent_specs.append(
                StageAgent(agent_name="explorer_fno_positioning", persona_version="v1",
                           on_iteration_failure="skip_one")
            )

        symbol = candidate.get("symbol", "unknown")
        sub_outputs = await run_subagents_parallel(
            runner=self,
            subagent_specs=subagent_specs,
            shared_input=candidate,
            ctx=ctx,
            aggregate_into=f"_explorer_{symbol}",
        )

        aggregator_input = {"sub_outputs": sub_outputs, "candidate": candidate}
        aggregator_inv = _AgentInvocation(
            stage_agent=StageAgent(agent_name="explorer_aggregator", persona_version="v1"),
            item=aggregator_input,
            item_index=invocation.item_index,
        )
        return await self._invoke_agent(aggregator_inv, ctx)

    # ------------------------------------------------------------------
    # API call + retries
    # ------------------------------------------------------------------

    async def _execute_with_retries(
        self,
        spec: AgentSpec,
        api_request: dict,
        agent_run_id: str,
        ctx: WorkflowContext,
    ) -> AgentRunResult:
        """Retry loop with transient and validation budgets."""
        transient_attempt = 0
        validation_attempt = 0
        last_error: str = ""
        used_model = spec.model
        api_request_current = api_request
        _used_fallback = False

        while transient_attempt <= spec.max_retries_transient:
            response = None  # ensure name is bound before any exception can reference it
            try:
                t0 = time.monotonic()
                import types as _types
                _extra_empty = _types.SimpleNamespace(
                    input_tokens=0, output_tokens=0,
                    cache_read_input_tokens=0, cache_creation_input_tokens=0,
                )
                extra_usage = _extra_empty
                extra_tool_calls: list[dict] = []

                if spec.tools and self.anthropic:
                    # Multi-turn: data tool gathering then forced output tool
                    response, extra_usage, extra_tool_calls = await self._run_data_tool_pre_loop(
                        spec, api_request_current, agent_run_id, ctx
                    )
                elif spec.stream_response and self.anthropic:
                    response = await self._stream_response(
                        spec, api_request_current, agent_run_id, ctx
                    )
                elif self.anthropic:
                    response = await asyncio.wait_for(
                        self.anthropic.messages.create(**api_request_current),
                        timeout=spec.timeout_seconds,
                    )
                else:
                    # Test / offline mode: return a stub failure
                    return self._make_failed_result(
                        spec, agent_run_id, used_model,
                        "No Anthropic client configured", "no_client"
                    )
                duration_ms = int((time.monotonic() - t0) * 1000)

                tool_call = self._extract_tool_call(response, spec.output_tool)
                if tool_call is None and spec.output_tool is not None:
                    raise OutputValidationError(
                        f"Model did not call required tool {spec.output_tool!r}"
                    )

                usage = response.usage
                cost = compute_cost(used_model, usage)
                # Add cost for intermediate data-tool turns
                cost += compute_cost(used_model, extra_usage)

                audit_id = await self._write_llm_audit_log(
                    spec, used_model, api_request_current, response, ctx, agent_run_id
                )

                candidate = AgentRunResult(
                    agent_run_id=agent_run_id,
                    agent_name=spec.name,
                    persona_version=spec.persona_version,
                    model_used=used_model,
                    status="succeeded",
                    output=tool_call.input if tool_call else self._extract_text(response),
                    raw_output=tool_call.input if tool_call else None,
                    cost_usd=cost,
                    input_tokens=(getattr(usage, "input_tokens", 0) or 0)
                                 + (getattr(extra_usage, "input_tokens", 0) or 0),
                    output_tokens=(getattr(usage, "output_tokens", 0) or 0)
                                  + (getattr(extra_usage, "output_tokens", 0) or 0),
                    cache_read_tokens=(getattr(usage, "cache_read_input_tokens", 0) or 0)
                                      + (getattr(extra_usage, "cache_read_input_tokens", 0) or 0),
                    cache_creation_tokens=(getattr(usage, "cache_creation_input_tokens", 0) or 0)
                                          + (getattr(extra_usage, "cache_creation_input_tokens", 0) or 0),
                    duration_ms=duration_ms,
                    llm_audit_log_id=audit_id,
                    tool_calls_made=extra_tool_calls,
                )

                # Run semantic validator inside the retry loop so that rejection
                # raises OutputValidationError and triggers a repair-and-retry.
                if spec.output_validator:
                    validated = await self._validate_output(spec, candidate, ctx)
                    if validated.status == "rejected_by_guardrail":
                        raise OutputValidationError(
                            "; ".join(validated.validation_errors or [str(validated.error)])
                        )

                return candidate

            except OutputValidationError as e:
                validation_attempt += 1
                last_error = str(e)
                if validation_attempt > spec.max_retries_validation:
                    return self._make_failed_result(
                        spec, agent_run_id, used_model, last_error, "validation_exhausted"
                    )
                # Repair-prompt — `response` is guaranteed bound (set to None at loop start)
                api_request_current = self._build_repair_request(
                    api_request_current, response, e
                )

            except (asyncio.TimeoutError,) as e:
                transient_attempt += 1
                last_error = f"TimeoutError: {e}"
                log.warning(f"[{spec.name}] timeout (attempt {transient_attempt})")
                if transient_attempt > spec.max_retries_transient:
                    if spec.fallback_model and spec.on_failure == "degrade" and not _used_fallback:
                        _used_fallback = True
                        used_model = spec.fallback_model
                        api_request_current = {**api_request_current, "model": spec.fallback_model}
                        transient_attempt = 0
                        continue
                    return self._make_failed_result(
                        spec, agent_run_id, used_model, last_error, "timeout_exhausted"
                    )
                await asyncio.sleep(min(2 ** transient_attempt, self.config.transient_retry_max_backoff_s))

            except Exception as e:
                transient_attempt += 1
                last_error = f"{type(e).__name__}: {e}"
                log.warning(
                    f"[{spec.name}] transient failure "
                    f"(attempt {transient_attempt}/{spec.max_retries_transient}): {last_error}"
                )
                if transient_attempt > spec.max_retries_transient:
                    if spec.fallback_model and spec.on_failure == "degrade" and not _used_fallback:
                        log.info(f"[{spec.name}] retrying with fallback {spec.fallback_model!r}")
                        _used_fallback = True
                        used_model = spec.fallback_model
                        api_request_current = {**api_request_current, "model": spec.fallback_model}
                        transient_attempt = 0
                        continue
                    return self._make_failed_result(
                        spec, agent_run_id, used_model, last_error, "transient_exhausted"
                    )
                await asyncio.sleep(min(2 ** transient_attempt, self.config.transient_retry_max_backoff_s))

        return self._make_failed_result(spec, agent_run_id, used_model, last_error, "loop_exit")

    async def _stream_response(
        self,
        spec: AgentSpec,
        api_request: dict,
        agent_run_id: str,
        ctx: "WorkflowContext | None" = None,
    ):
        """Stream Opus response; writes partial text to agent_runs for live monitoring."""
        partial_buf: list[str] = []
        last_write = time.monotonic()
        WRITE_INTERVAL = 3.0  # seconds between partial DB flushes

        async with self.anthropic.messages.stream(**api_request) as stream:
            async for event in stream:
                if ctx and hasattr(event, "delta") and hasattr(event.delta, "text"):
                    partial_buf.append(event.delta.text)
                    if time.monotonic() - last_write >= WRITE_INTERVAL and partial_buf:
                        partial_text = "".join(partial_buf)
                        asyncio.create_task(
                            self._write_partial_agent_output(agent_run_id, partial_text)
                        )
                        last_write = time.monotonic()
            return await stream.get_final_message()

    async def _write_partial_agent_output(self, agent_run_id: str, partial_text: str) -> None:
        """Write streaming partial output to agent_runs for live operator monitoring."""
        try:
            async with self.db_session_factory() as db:
                await db.execute(
                    text("UPDATE agent_runs SET output = :p WHERE id = :id"),
                    {"p": json.dumps({"partial": partial_text}), "id": agent_run_id},
                )
                await db.commit()
        except Exception as e:
            log.debug("Partial stream write failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Agentic data-tool dispatch loop
    # ------------------------------------------------------------------

    async def _run_data_tool_pre_loop(
        self,
        spec: AgentSpec,
        api_request: dict,
        agent_run_id: str,
        ctx: "WorkflowContext",
        max_tool_turns: int = 10,
    ) -> tuple[Any, Any, list[dict]]:
        """Execute data tool calls in a loop until the model calls the output tool.

        Returns (final_response, accumulated_extra_usage, tool_calls_made).
        The extra_usage is a SimpleNamespace compatible with compute_cost().
        """
        import types as _types

        messages = list(api_request["messages"])
        # Allow any tool on intermediate turns
        req = {**api_request, "messages": messages}
        if spec.tools and spec.output_tool:
            req["tool_choice"] = {"type": "auto"}

        acc_input = acc_output = acc_cache_r = acc_cache_c = 0
        tool_calls_made: list[dict] = []

        for turn in range(max_tool_turns + 1):
            is_last = turn >= max_tool_turns
            if is_last and spec.output_tool:
                req = {**req, "tool_choice": {"type": "tool", "name": spec.output_tool}}

            if spec.stream_response:
                response = await self._stream_response(spec, req, agent_run_id, ctx)
            else:
                response = await asyncio.wait_for(
                    self.anthropic.messages.create(**req),
                    timeout=spec.timeout_seconds,
                )

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            output_called = spec.output_tool and any(b.name == spec.output_tool for b in tool_uses)

            if output_called or is_last or getattr(response, "stop_reason", None) == "end_turn":
                return response, _types.SimpleNamespace(
                    input_tokens=acc_input, output_tokens=acc_output,
                    cache_read_input_tokens=acc_cache_r, cache_creation_input_tokens=acc_cache_c,
                ), tool_calls_made

            if not tool_uses:
                return response, _types.SimpleNamespace(
                    input_tokens=acc_input, output_tokens=acc_output,
                    cache_read_input_tokens=acc_cache_r, cache_creation_input_tokens=acc_cache_c,
                ), tool_calls_made

            # Accumulate this intermediate turn's tokens
            u = response.usage
            acc_input += getattr(u, "input_tokens", 0) or 0
            acc_output += getattr(u, "output_tokens", 0) or 0
            acc_cache_r += getattr(u, "cache_read_input_tokens", 0) or 0
            acc_cache_c += getattr(u, "cache_creation_input_tokens", 0) or 0

            # Dispatch each data tool with per-tool timeout
            tool_results = []
            for tb in tool_uses:
                result = await self._dispatch_data_tool(tb, spec, agent_run_id, ctx)
                tool_calls_made.append({"tool": tb.name, "input_keys": list(getattr(tb, "input", {}).keys())})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb.id,
                    "content": json.dumps(result, default=str),
                })

            messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            req = {**req, "messages": messages}

        # Unreachable; satisfies type checker
        return response, _types.SimpleNamespace(  # type: ignore[return-value]
            input_tokens=acc_input, output_tokens=acc_output,
            cache_read_input_tokens=acc_cache_r, cache_creation_input_tokens=acc_cache_c,
        ), tool_calls_made

    async def _dispatch_data_tool(
        self, tool_use_block: Any, spec: AgentSpec, agent_run_id: str, ctx: "WorkflowContext"
    ) -> dict:
        """Execute one data tool with per-tool timeout. Never raises — returns error dict on failure."""
        from src.agents.runtime.spec import ToolContext

        tool_name = tool_use_block.name
        td = self._tool_registry.get(tool_name)
        if td is None:
            return {"error": f"Unknown tool: {tool_name}"}

        tool_ctx = ToolContext(
            workflow_run_id=ctx.workflow_run_id,
            agent_run_id=agent_run_id,
            agent_name=spec.name,
            db=None,  # executor opens its own session via db_session_factory
            redis=self.redis,
            as_of=ctx.as_of,
            is_replay=ctx.is_replay,
        )
        try:
            result = await asyncio.wait_for(
                td.executor(getattr(tool_use_block, "input", {}), tool_ctx),
                timeout=td.timeout_seconds,
            )
            return result
        except asyncio.TimeoutError:
            log.warning("[%s] tool %r timed out after %ds", spec.name, tool_name, td.timeout_seconds)
            return {"error": f"{tool_name} timed out after {td.timeout_seconds}s"}
        except Exception as e:
            log.warning("[%s] tool %r failed: %s", spec.name, tool_name, e)
            return {"error": f"{tool_name} failed: {type(e).__name__}: {e}"}

    # ------------------------------------------------------------------
    # Output validation
    # ------------------------------------------------------------------

    async def _validate_output(
        self, spec: AgentSpec, result: AgentRunResult, ctx: WorkflowContext
    ) -> AgentRunResult:
        """Apply the agent's Pydantic validator for semantic checks."""
        validator_cls = self._validator_registry.get(spec.output_validator)
        if not validator_cls:
            return result
        try:
            validator_cls(**(result.output or {}))
            return result
        except Exception as e:
            errors = getattr(e, "errors", lambda: [str(e)])()
            return AgentRunResult(
                **{
                    **result.__dict__,
                    "status": "rejected_by_guardrail",
                    "validation_errors": [str(err) for err in errors],
                    "error": f"output_validator={spec.output_validator}: {errors[0]}",
                }
            )

    # ------------------------------------------------------------------
    # Final cross-agent validators
    # ------------------------------------------------------------------

    async def _run_final_validators(
        self, workflow_spec: WorkflowSpec, ctx: WorkflowContext
    ) -> list[dict]:
        """Run cross-agent validators on the composed prediction before commit."""
        outcomes: list[dict] = []
        candidate_prediction = self._compose_prediction_from_judge(ctx)

        for validator_name in workflow_spec.final_validators:
            validator_cls = self._validator_registry.get(validator_name)
            if not validator_cls:
                outcomes.append({
                    "validator": validator_name, "outcome": "missing",
                    "error": "Validator not found in registry",
                })
                continue
            try:
                validator_cls(**candidate_prediction)
                outcomes.append({"validator": validator_name, "outcome": "passed"})
            except Exception as e:
                errors = getattr(e, "errors", lambda: [{"msg": str(e)}])()
                first_err = errors[0] if errors else {"msg": str(e)}
                severity = self._validator_severity(validator_name, first_err)
                outcomes.append({
                    "validator": validator_name,
                    "outcome": "rejected" if severity == "hard" else "caveat",
                    "error": str(first_err),
                })
        return outcomes

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _persist_predictions(
        self, ctx: WorkflowContext, validator_outcomes: list[dict]
    ) -> list[dict]:
        """Write agent_predictions row(s) from the judge verdict."""
        any_rejected = any(o["outcome"] == "rejected" for o in validator_outcomes)
        any_caveat = any(o["outcome"] == "caveat" for o in validator_outcomes)

        if validator_outcomes:
            first_bad = next(
                (o for o in validator_outcomes if o["outcome"] in ("rejected", "caveat")), None
            )
        else:
            first_bad = None

        guardrail_status = (
            f"rejected:{first_bad['validator']}" if any_rejected
            else f"caveat:{first_bad['validator']}" if (any_caveat and first_bad)
            else "passed"
        )

        judge_verdict = ctx.stage_outputs.get("judge_verdict") or {}
        allocation = judge_verdict.get("allocation", [])
        prompt_versions = self._collect_prompt_versions(ctx)
        model_used = ctx.agent_run_results[-1].model_used if ctx.agent_run_results else "unknown"

        persisted: list[dict] = []
        async with self.db_session_factory() as db:
            for alloc in allocation:
                prediction_id = str(uuid4())
                await db.execute(
                    text("""
                        INSERT INTO agent_predictions (
                            id, workflow_run_id, as_of, asset_class, symbol_or_underlying,
                            decision, rationale, conviction, expected_pnl_pct, max_loss_pct,
                            target_price, stop_price, horizon, model_used, prompt_versions,
                            guardrail_status, kill_switches, judge_output, created_at
                        ) VALUES (
                            :id, :wfr, :as_of, :ac, :sym, :dec, :rat, :con,
                            :exp, :max, :tgt, :stp, :hor, :mdl, :pv,
                            :gs, :ks, :jo, NOW()
                        )
                    """),
                    {
                        "id": prediction_id,
                        "wfr": ctx.workflow_run_id,
                        "as_of": ctx.as_of,
                        "ac": alloc.get("asset_class", "equity"),
                        "sym": alloc.get("underlying_or_symbol", ""),
                        "dec": alloc.get("decision", judge_verdict.get("decision_summary", "")),
                        "rat": judge_verdict.get("decision_summary"),
                        "con": alloc.get("conviction", judge_verdict.get(
                            "calibration_self_check", {}
                        ).get("confidence_in_allocation")),
                        "exp": judge_verdict.get("expected_book_pnl_pct"),
                        "max": judge_verdict.get("max_drawdown_tolerated_pct"),
                        "tgt": None,
                        "stp": None,
                        "hor": alloc.get("horizon"),
                        "mdl": model_used,
                        "pv": json.dumps(prompt_versions),
                        "gs": guardrail_status,
                        "ks": json.dumps(judge_verdict.get("kill_switches", [])),
                        "jo": json.dumps(judge_verdict),
                    },
                )
                persisted.append({
                    "id": prediction_id,
                    **alloc,
                    "guardrail_status": guardrail_status,
                })
            await db.commit()

        ctx.stage_outputs["prediction_id"] = persisted[0]["id"] if persisted else None
        return persisted

    async def _persist_agent_run_started(
        self,
        agent_run_id: str,
        sa: StageAgent,
        ctx: WorkflowContext,
        estimated_input_tokens: int,
        item_index: int = 0,
    ) -> None:
        async with self.db_session_factory() as db:
            await db.execute(
                text("""
                    INSERT INTO agent_runs (
                        id, workflow_run_id, agent_name, persona_version, model,
                        status, started_at, estimated_input_tokens, iteration_index
                    ) VALUES (
                        :id, :wfr, :name, :ver, :model,
                        'running', NOW(), :est, :idx
                    )
                """),
                {
                    "id": agent_run_id,
                    "wfr": ctx.workflow_run_id,
                    "name": sa.agent_name,
                    "ver": sa.persona_version,
                    "model": "tbd",
                    "est": estimated_input_tokens,
                    "idx": item_index,
                },
            )
            await db.commit()

    async def _persist_agent_run_completed(
        self, result: AgentRunResult, ctx: WorkflowContext
    ) -> None:
        async with self.db_session_factory() as db:
            await db.execute(
                text("""
                    UPDATE agent_runs SET
                        status = :status,
                        model_used = :model,
                        output = :output,
                        cost_usd = :cost,
                        input_tokens = :in_tok,
                        output_tokens = :out_tok,
                        cache_read_tokens = :cache_r,
                        cache_creation_tokens = :cache_c,
                        duration_ms = :dur,
                        error = :error,
                        validation_errors = :validation_errors,
                        llm_audit_log_id = :audit_id,
                        completed_at = NOW()
                    WHERE id = :id
                """),
                {
                    "id": result.agent_run_id,
                    "status": result.status,
                    "model": result.model_used,
                    "output": json.dumps(result.output) if result.output else None,
                    "cost": result.cost_usd,
                    "in_tok": result.input_tokens,
                    "out_tok": result.output_tokens,
                    "cache_r": result.cache_read_tokens,
                    "cache_c": result.cache_creation_tokens,
                    "dur": result.duration_ms,
                    "error": result.error,
                    "validation_errors": json.dumps(result.validation_errors),
                    "audit_id": result.llm_audit_log_id,
                },
            )
            await db.commit()

    async def _create_workflow_run_row(
        self,
        db,
        workflow_run_id: str,
        workflow_spec: WorkflowSpec,
        params: dict,
        triggered_by: str,
        idempotency_key: str | None = None,
    ) -> None:
        await db.execute(
            text("""
                INSERT INTO workflow_runs (
                    id, workflow_name, version, status, triggered_by,
                    params, idempotency_key, started_at, created_at
                ) VALUES (
                    :id, :name, :ver, 'running', :tb,
                    :params, :idem, NOW(), NOW()
                )
            """),
            {
                "id": workflow_run_id,
                "name": workflow_spec.name,
                "ver": workflow_spec.version,
                "tb": triggered_by,
                "params": json.dumps(params, default=str),
                "idem": idempotency_key,
            },
        )

    async def _finalize_workflow_run(
        self,
        ctx: WorkflowContext,
        status: str,
        status_extended: str | None = None,
        predictions: list[dict] | None = None,
        validator_outcomes: list[dict] | None = None,
        error: str | None = None,
        short_circuit_reason: str | None = None,
    ) -> WorkflowRunResult:
        async with self.db_session_factory() as db:
            await db.execute(
                text("""
                    UPDATE workflow_runs SET
                        status = :status,
                        status_extended = :status_extended,
                        cost_usd = :cost,
                        total_tokens = :tokens,
                        error = :error,
                        completed_at = NOW()
                    WHERE id = :id
                """),
                {
                    "id": ctx.workflow_run_id,
                    "status": status,
                    "status_extended": status_extended,
                    "cost": ctx.cost_so_far_usd,
                    "tokens": ctx.tokens_so_far,
                    "error": error,
                },
            )
            await db.commit()

        return WorkflowRunResult(
            workflow_run_id=ctx.workflow_run_id,
            workflow_name=ctx.workflow_spec.name,
            status=status,
            status_extended=status_extended,
            cost_usd=ctx.cost_so_far_usd,
            total_tokens=ctx.tokens_so_far,
            agent_run_results=ctx.agent_run_results,
            predictions=predictions or [],
            validator_outcomes=validator_outcomes or [],
            stage_outputs=dict(ctx.stage_outputs),
            error=error,
            short_circuit_reason=short_circuit_reason,
        )

    @staticmethod
    def _safe_jsonb(data: object, limit: int = 65535) -> object:
        """Serialize to JSON and return as dict/list for JSONB, or None if too large.

        Truncating a JSON string before passing to a JSONB column causes a parse
        error. We skip the field entirely rather than risk a broken INSERT.
        """
        serialised = json.dumps(data, default=str)
        if len(serialised) > limit:
            return None  # omit oversized payloads — audit row still written
        return data  # pass native Python object; SQLAlchemy/asyncpg serialises to JSONB

    async def _write_llm_audit_log(
        self, spec: AgentSpec, model: str, api_request: dict,
        response, ctx: WorkflowContext, agent_run_id: str
    ) -> str | None:
        """Write one row to llm_audit_log; returns the new row's UUID."""
        try:
            log_id = str(uuid4())
            usage = response.usage
            response_content = [
                b.dict() if hasattr(b, "dict") else str(b) for b in response.content
            ]
            async with self.db_session_factory() as db:
                await db.execute(
                    text("""
                        INSERT INTO llm_audit_log (
                            id, caller, caller_ref_id, model, temperature,
                            prompt, response, tokens_in, tokens_out, latency_ms,
                            caller_tag, caller_meta, request_body, response_body,
                            cache_read_tokens, cache_creation_tokens, cost_usd
                        ) VALUES (
                            :id, :caller, NULL, :model, :temp,
                            :prompt, :resp, :in_tok, :out_tok, NULL,
                            :tag, :meta, :req, :res,
                            :cr, :cc, :cost
                        )
                    """),
                    {
                        "id": log_id,
                        "caller": f"agent.{spec.name}",
                        "model": model,
                        "temp": spec.temperature,
                        "prompt": json.dumps(api_request.get("messages", [])[:1], default=str)[:65535],
                        "resp": json.dumps(response_content, default=str)[:65535],
                        "in_tok": getattr(usage, "input_tokens", None),
                        "out_tok": getattr(usage, "output_tokens", None),
                        "tag": f"agent.{spec.name}",
                        "meta": json.dumps({
                            "workflow_run_id": ctx.workflow_run_id,
                            "agent_run_id": agent_run_id,
                            "persona_version": spec.persona_version,
                        }),
                        "req": self._safe_jsonb(api_request),
                        "res": self._safe_jsonb(response_content),
                        "cr": getattr(usage, "cache_read_input_tokens", 0) or 0,
                        "cc": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                        "cost": float(compute_cost(model, usage)),
                    },
                )
                await db.commit()
            return log_id
        except Exception as e:
            log.warning(f"Failed to write llm_audit_log: {e}")
            return None

    # ------------------------------------------------------------------
    # Prompt assembly helpers
    # ------------------------------------------------------------------

    def _build_agent_spec(self, agent_name: str, persona_version: str) -> AgentSpec:
        """Resolve AgentSpec from PERSONA_MANIFEST."""
        versions = self._persona_manifest.get(agent_name, {})
        persona_def = versions.get(persona_version)
        if persona_def is None:
            raise ValueError(
                f"Agent {agent_name!r} version {persona_version!r} not in PERSONA_MANIFEST"
            )
        _AGENT_SPEC_FIELDS = {
            "model", "fallback_model", "tools", "output_tool",
            "max_input_tokens", "max_output_tokens", "temperature",
            "max_retries_transient", "max_retries_validation", "on_failure",
            "timeout_seconds", "cache_system", "stream_response",
            "cost_class", "output_validator",
        }
        spec_kwargs = {k: v for k, v in persona_def.items() if k in _AGENT_SPEC_FIELDS}
        return AgentSpec(
            name=agent_name,
            persona_version=persona_version,
            **spec_kwargs,
        )

    def _assemble_prompt(
        self, spec: AgentSpec, invocation: _AgentInvocation, ctx: WorkflowContext
    ) -> list[dict]:
        """Assemble the user-turn messages for the API call."""
        item = invocation.item
        if item is None:
            # Build from stage_outputs context
            item = dict(ctx.stage_outputs)
        if isinstance(item, dict):
            content = json.dumps(item, default=str, indent=2)
        else:
            content = str(item)
        return [{"role": "user", "content": content}]

    def _estimate_input_tokens(self, spec: AgentSpec, messages: list[dict]) -> int:
        """Rough token estimate: len(text) / 3.5 (over-estimates, so conservative)."""
        total_chars = sum(len(json.dumps(m)) for m in messages)
        return int(total_chars / 3.5)

    def _build_api_request(
        self, spec: AgentSpec, messages: list[dict], ctx: WorkflowContext
    ) -> dict:
        """Build the kwargs dict for anthropic.messages.create()."""
        from src.agents.personas import PERSONA_MANIFEST
        versions = PERSONA_MANIFEST.get(spec.name, {})
        persona_def = versions.get(spec.persona_version, {})
        system_prompt = persona_def.get("system_prompt", f"You are {spec.name}.")

        system_block: list[dict] = [
            {
                "type": "text",
                "text": system_prompt,
                **({"cache_control": {"type": "ephemeral"}} if spec.cache_system else {}),
            }
        ]

        from src.agents.personas import OUTPUT_TOOL_SCHEMAS

        tools: list[dict] = []
        if spec.output_tool and spec.output_tool in OUTPUT_TOOL_SCHEMAS:
            tools.append(OUTPUT_TOOL_SCHEMAS[spec.output_tool])

        for tool_name in spec.tools:
            td = self._tool_registry.get(tool_name)
            if td:
                tools.append(td.json_schema)

        req: dict = {
            "model": spec.model,
            "max_tokens": spec.max_output_tokens,
            "temperature": spec.temperature,
            "system": system_block,
            "messages": messages,
        }
        if tools:
            req["tools"] = tools
        if spec.output_tool:
            req["tool_choice"] = {"type": "tool", "name": spec.output_tool}
        return req

    def _build_repair_request(
        self, original_request: dict, prior_response, validation_error
    ) -> dict:
        """Rebuild the request to retry after a validation failure."""
        if prior_response is None:
            return original_request
        try:
            prior_tool_use = next(
                b for b in prior_response.content if getattr(b, "type", None) == "tool_use"
            )
        except StopIteration:
            return original_request

        repair_messages = [
            *original_request["messages"],
            {"role": "assistant", "content": prior_response.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": prior_tool_use.id,
                        "content": (
                            f"Your output failed validation: {validation_error}\n"
                            f"Please re-emit the {original_request.get('tool_choice', {}).get('name', 'output')} "
                            f"tool call with corrections."
                        ),
                        "is_error": True,
                    }
                ],
            },
        ]
        return {**original_request, "messages": repair_messages}

    @staticmethod
    def _extract_tool_call(response, output_tool: str | None):
        """Extract the forced tool-use block from the response."""
        if not output_tool or response is None:
            return None
        for block in (response.content or []):
            if getattr(block, "type", None) == "tool_use" and block.name == output_tool:
                return block
        return None

    @staticmethod
    def _extract_text(response) -> dict | None:
        """Fallback: extract text content when no output_tool is forced."""
        if response is None:
            return None
        for block in (response.content or []):
            if getattr(block, "type", None) == "text":
                return {"text": block.text}
        return None

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _should_short_circuit(self, stage: WorkflowStage, ctx: WorkflowContext) -> bool:
        """Return True if the brain_triage stage produced skip_today=true."""
        if stage.stage_name != "brain_triage":
            return False
        triage = ctx.stage_outputs.get("triage") or {}
        return bool(triage.get("skip_today"))

    def _evaluate_condition(self, condition: str | None, ctx: WorkflowContext) -> bool:
        if not condition:
            return True
        # Use ast.literal_eval for simple equality checks, or a restricted namespace.
        # We never load WorkflowSpecs from untrusted sources, but restrict builtins
        # anyway as a defence-in-depth measure against future configuration changes.
        try:
            import ast
            restricted_globals = {"__builtins__": {}, "stage_outputs": ctx.stage_outputs}
            return bool(eval(  # noqa: S307
                compile(ast.parse(condition, mode="eval"), "<condition>", "eval"),
                restricted_globals,
            ))
        except Exception as e:
            log.warning(f"Condition eval failed: {e}")
            return False

    def _resolve_iteration(self, iteration_source: str, ctx: WorkflowContext) -> list:
        """Resolve an iteration_source expression to a list of items."""
        parts = iteration_source.split("+")
        result: list = []
        for part in parts:
            keys = part.strip().split(".")
            val = ctx.stage_outputs
            for k in keys:
                if isinstance(val, dict):
                    val = val.get(k, [])
                else:
                    val = []
                    break
            if isinstance(val, list):
                result.extend(val)
        return result

    def _project_stage_cost(self, stage: WorkflowStage, ctx: WorkflowContext) -> Decimal:
        total = Decimal("0")
        for sa in stage.agents:
            try:
                spec = self._build_agent_spec(sa.agent_name, sa.persona_version)
            except ValueError:
                continue
            per_call = project_agent_cost(
                spec.model, spec.max_input_tokens, spec.max_output_tokens
            )
            n = 5 if sa.iteration_source else 1
            total += per_call * n
        return total

    def _collect_prompt_versions(self, ctx: WorkflowContext) -> dict[str, str]:
        versions: dict[str, str] = {}
        for ar in ctx.agent_run_results:
            versions[ar.agent_name] = ar.persona_version
        return versions

    def _compose_prediction_from_judge(self, ctx: WorkflowContext) -> dict:
        judge_verdict = ctx.stage_outputs.get("judge_verdict") or {}
        return {
            "allocation": judge_verdict.get("allocation", []),
            "decision_summary": judge_verdict.get("decision_summary", ""),
            "conviction": judge_verdict.get("calibration_self_check", {}).get(
                "confidence_in_allocation", 0.5
            ),
            "expected_book_pnl_pct": judge_verdict.get("expected_book_pnl_pct"),
            "max_drawdown_tolerated_pct": judge_verdict.get("max_drawdown_tolerated_pct"),
            "kill_switches": judge_verdict.get("kill_switches", []),
        }

    @staticmethod
    def _validator_severity(validator_name: str, error: dict) -> str:
        """Hard validators fail the prediction; soft validators caveat it.

        Pydantic v2 field_validator errors carry the validator function name in
        the 'ctx.error' or message string rather than in 'loc'.  We match against
        known hard-validator substrings in the error message for reliability.
        """
        hard_msg_markers = {
            "sums to",           # capital_pct_sums_to_at_most_100
            "exceeds 40%",       # no_single_leg_over_40_pct (non-cash)
            "max_drawdown",      # kill_switches_match_drawdown
            "expected_pnl",      # expected_pnl_is_positive
        }
        msg = error.get("msg", "") if isinstance(error, dict) else ""
        if any(marker in msg for marker in hard_msg_markers):
            return "hard"
        return "soft"

    def _is_overridden(self, spec: AgentSpec, ctx: WorkflowContext) -> bool:
        """Return True if this agent should make a live API call rather than serve from cache.

        An agent is live if it has an explicit persona_version_override, OR if
        from_agent is set and we have reached or passed that agent in the pipeline.
        """
        if spec.name in ctx.persona_version_overrides:
            return True
        if ctx.from_agent:
            if spec.name == ctx.from_agent or ctx._replay_live_mode:
                ctx._replay_live_mode = True  # flip on permanently once reached
                return True
        return False

    async def _fetch_from_audit_log(
        self, spec: AgentSpec, ctx: WorkflowContext, invocation: _AgentInvocation
    ) -> dict | None:
        """Fetch a prior agent_run's output from llm_audit_log for replay."""
        try:
            async with self.db_session_factory() as db:
                result = await db.execute(
                    text("""
                        SELECT response_body
                        FROM llm_audit_log
                        WHERE caller_tag = :tag
                          AND caller_meta->>'workflow_run_id' = :wfr
                          AND caller_meta->>'persona_version' = :pv
                        ORDER BY created_at
                        LIMIT 1
                    """),
                    {
                        "tag": f"agent.{spec.name}",
                        "wfr": ctx.params.get("original_workflow_run_id", ctx.workflow_run_id),
                        "pv": spec.persona_version,
                    },
                )
                row = result.fetchone()
                if row and row[0]:
                    return row[0]
        except Exception as e:
            log.warning(f"Replay audit log fetch failed: {e}")
        return None

    def _materialize_result_from_audit(
        self, cached: list | dict, spec: AgentSpec, agent_run_id: str
    ) -> AgentRunResult:
        """Convert a cached audit log response_body (list of content blocks) into an AgentRunResult.

        response_body is stored as a JSONB array of Anthropic content blocks.
        We extract the tool_use block's input, falling back to text content.
        """
        tool_input: dict | None = None
        content_blocks = cached if isinstance(cached, list) else [cached]
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_input = block.get("input")
                break
        if tool_input is None:
            # Fallback: concatenate text blocks
            tool_input = {"_text": " ".join(
                b.get("text", "") for b in content_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )}
        return AgentRunResult(
            agent_run_id=agent_run_id,
            agent_name=spec.name,
            persona_version=spec.persona_version,
            model_used=spec.model + " (replayed)",
            status="succeeded",
            output=tool_input,
            raw_output=tool_input,
            cost_usd=Decimal("0"),
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            duration_ms=0,
        )

    @staticmethod
    def _make_failed_result(
        spec: AgentSpec, agent_run_id: str, model: str, error: str, reason: str
    ) -> AgentRunResult:
        return AgentRunResult(
            agent_run_id=agent_run_id,
            agent_name=spec.name,
            persona_version=spec.persona_version,
            model_used=model,
            status="failed",
            output=None,
            raw_output=None,
            cost_usd=Decimal("0"),
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            duration_ms=0,
            error=f"[{reason}] {error}",
        )

    async def _idempotency_taken(self, key: str) -> bool:
        """Return True if this idempotency_key was already used.

        Checks the DB first (permanent record) then uses Redis as a fast-path
        lock for in-flight runs. Checking DB first prevents false positives
        where a legitimate retry within the Redis TTL window is wrongly blocked.
        """
        try:
            async with self.db_session_factory() as db:
                result = await db.execute(
                    text("SELECT 1 FROM workflow_runs WHERE idempotency_key = :k LIMIT 1"),
                    {"k": key},
                )
                if result.fetchone():
                    return True
        except Exception as e:
            log.warning(f"Idempotency DB check failed: {e}")

        # Redis fast-path: block duplicate submissions within the same TTL window
        try:
            result = await self.redis.set(f"laabh:idem:{key}", "1", nx=True, ex=300)
            return result is None  # None → key already existed in Redis
        except Exception:
            return False

    async def _check_kill_switch(self) -> bool:
        try:
            return await self.redis.get("laabh:kill_switch") == b"1"
        except Exception:
            return False

    async def _alert_telegram(self, msg: str, severity: str = "warning") -> None:
        if not self.telegram or not self.config.telegram_alert_on_failure:
            return
        try:
            await self.telegram.send(
                chat_id=self.config.telegram_chat_id,
                text=f"[{severity.upper()}] Laabh runtime\n{msg}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"Telegram alert failed (non-fatal): {e}")
