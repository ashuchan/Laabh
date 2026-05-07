# CLAUDE-AGENTS-RUNTIME.md — Workflow Runtime and Orchestration

**Audience:** ashusaxe007@gmail.com
**Date:** 2026-05-07
**Status:** Production runtime spec (3rd of 4 change-sets)
**Scope:** The execution layer that turns the prompts and tools (change-set #2)
into running workflows. Specifies AgentSpec, WorkflowSpec, WorkflowRunner,
the structured-output contract, fallback/retry policy, replay semantics,
token-budget enforcement, the cost circuit breaker, the AgentRun writer, and
the cross-agent validators integration.

This document is implementable as-is. Every class and function below has a
concrete contract; the only TBD is the SQL-level executors of the tools
themselves (which live in `src/agents/tools/*.py` and are project-internal).

---

## §0 Scope and non-goals

**In scope:**
- Pydantic models for `AgentSpec`, `WorkflowSpec`, `ToolContext`, `AgentRunResult`
- `WorkflowRunner` class — the single entry point for executing workflows
- Structured-output enforcement via Anthropic forced tool-use
- Fallback policy: primary model → fallback model on transient failure
- Replay-only resumability (no in-flight resume)
- Token-budget enforcement per-agent and per-workflow
- Cost circuit breaker (USD-denominated)
- Atomic `agent_runs` row writer with `llm_audit_log` linkage
- Sub-agent parallel fan-out helper
- Integration with the cross-agent Pydantic validators from change-set #2
- Reference workflow definitions for `predict_today_*` and `evaluate_yesterday`
- Operational concerns: Telegram failure alerts, idempotency keys, orphan reconciliation

**Out of scope (explicitly):**
- The web UI layer that triggers workflows (FastAPI routers consume `WorkflowRunner` — that's a separate slice)
- The mobile app's workflow status display (consumes `workflow_runs.status_extended`)
- The auto-trader that consumes `agent_predictions` (downstream system)
- Tool executor SQL (lives in `src/agents/tools/*.py`)

---

## §1 Domain models

### 1.1 `AgentSpec` — declarative configuration of one agent persona

```python
# src/agents/runtime/spec.py
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class AgentSpec:
    """Declarative spec for one agent invocation. Loaded from PERSONA_MANIFEST
    at runtime; an AgentSpec instance is immutable for the duration of a
    workflow run.

    Identity
    --------
    name: persona ID (e.g. "fno_expert"). Must exist in PERSONA_MANIFEST.
    persona_version: which version of the prompt to use (e.g. "v1", "v2").
                     Stored in agent_runs.persona_version and
                     agent_predictions.prompt_versions[name].

    Models
    ------
    model: primary model ID for the API call. Examples:
           - "claude-haiku-4-5-20251001"
           - "claude-sonnet-4-6"
           - "claude-opus-4-7"
    fallback_model: model to retry with on transient API failures (overload,
                    rate-limit, 5xx). Should be a different model class —
                    if Sonnet fails, fall back to Haiku, not back to Sonnet.

    Tool wiring
    -----------
    tools: list of tool names that must be available in TOOL_REGISTRY. The
           runtime injects matching ToolDefinition objects.
    output_tool: the *forced* tool name. The runtime sets
                 tool_choice={"type": "tool", "name": output_tool} on every
                 call. Without an output tool, the agent is free-form (rare —
                 only used for the editor's final critique).

    Token economics
    ---------------
    max_input_tokens: ceiling on assembled prompt + history. WorkflowRunner
                      raises BudgetExceeded if exceeded BEFORE making the API
                      call. Truncation policy is the agent's responsibility —
                      this is a hard ceiling.
    max_output_tokens: passed to the API as max_tokens.
    temperature: passed verbatim. Default 0.0 for deterministic agents,
                 0.1 for interpretive agents (news_finder, experts).

    Reliability
    -----------
    max_retries_transient: API-level retry budget for transient errors
                           (network, 5xx, overload). Default 3.
    max_retries_validation: schema-validation retry budget. If the model
                            returns a tool call that fails Pydantic validation,
                            the runtime can re-prompt with the validator's
                            error message attached. Default 2.
    on_failure: behavior when both retry budgets are exhausted.
                - "skip": record the failure, return None, continue workflow
                - "abort": fail the workflow_run
                - "degrade": call fallback_model with reduced context
    timeout_seconds: hard wall-clock ceiling. Default 60 (Sonnet/Haiku),
                     180 (Opus with streaming).

    Caching & streaming
    -------------------
    cache_system: whether to set cache_control on the system prompt.
                  Default True (always cached for cost reduction).
    stream_response: whether to stream the API response. True for Opus only.

    Cost class
    ----------
    cost_class: "cheap" | "medium" | "expensive". Determines the workflow's
                cost-circuit-breaker behavior (the runner pre-flights total
                cost projection from these labels and aborts before it spends).
    """
    name: str
    persona_version: str
    model: str
    fallback_model: str | None
    tools: tuple[str, ...]                        # frozen tuple, hashable
    output_tool: str | None
    max_input_tokens: int
    max_output_tokens: int
    temperature: float = 0.0
    max_retries_transient: int = 3
    max_retries_validation: int = 2
    on_failure: Literal["skip", "abort", "degrade"] = "abort"
    timeout_seconds: int = 60
    cache_system: bool = True
    stream_response: bool = False
    cost_class: Literal["cheap", "medium", "expensive"] = "medium"
    output_validator: str | None = None           # name of Pydantic class in validators.py
```

### 1.2 `WorkflowSpec` — the chain of agents

```python
@dataclass(frozen=True)
class WorkflowSpec:
    """Declarative spec for one workflow type. Multiple instances (workflow_runs)
    of the same WorkflowSpec can run independently.

    Identity
    --------
    name: workflow ID (e.g. "predict_today_combined").
    version: schema version of THIS workflow. When the agent chain or
             cross-agent validators change in a backward-incompatible way,
             bump this. Stored in workflow_runs.version.

    Topology
    --------
    stages: ordered list of WorkflowStage. Each stage is either a single
            agent, a parallel fan-out, or a conditional branch. Stages run
            sequentially; agents within a parallel stage run concurrently.

    Defaults
    --------
    default_params: parameters merged with caller-supplied params at
                    runtime (e.g. capital_base_mode, target_pnl_pct).
    cost_ceiling_usd: workflow-level $-budget. Runner aborts if projected
                     or actual cost exceeds.
    token_ceiling: workflow-level token-budget. Sum across all agent_runs.

    Cross-agent validators
    ----------------------
    final_validators: list of Pydantic class names from validators.py that
                      run on the FINAL agent_predictions row before commit.
                      Failures route to status_extended='succeeded_with_caveats'
                      and the row is committed with guardrail_status set.
    """
    name: str
    version: str
    stages: tuple["WorkflowStage", ...]
    default_params: dict[str, Any] = field(default_factory=dict)
    cost_ceiling_usd: Decimal = Decimal("5.0")
    token_ceiling: int = 150_000
    final_validators: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowStage:
    """One stage of a workflow. A stage runs to completion before the next
    stage begins. Within a stage, multiple agents can run in parallel."""
    stage_name: str                                # for logging/telemetry
    kind: Literal["sequential", "parallel", "conditional"]
    agents: tuple["StageAgent", ...]
    condition: str | None = None                   # for "conditional" stages — Python expr on workflow context


@dataclass(frozen=True)
class StageAgent:
    """One agent within a stage. The same agent persona may appear multiple
    times in a stage (e.g. fno_expert called once per F&O candidate)."""
    agent_name: str                                # PERSONA_MANIFEST key
    persona_version: str = "v1"
    iteration_source: str | None = None            # name of context var holding list to iterate
    on_iteration_failure: Literal["skip_one", "abort_stage", "abort_workflow"] = "skip_one"
    output_key: str = ""                           # where to store output in workflow context
```

### 1.3 `ToolContext` — runtime context for tool execution

```python
@dataclass
class ToolContext:
    """Passed to every tool executor. Provides DB connections, auth context,
    and the workflow_run's metadata so tools can scope queries correctly."""
    workflow_run_id: str                           # UUID
    agent_run_id: str                              # UUID — the calling agent_run
    agent_name: str
    db: "AsyncSession"                             # SQLAlchemy session
    redis: "Redis"                                 # for cache lookups
    as_of: "datetime"                              # IST timestamp anchor
    is_replay: bool                                # if True, tools should serve from llm_audit_log
    instrument_context: dict[str, Any] = field(default_factory=dict)
    cost_so_far_usd: Decimal = Decimal("0")        # mutable — runner updates
```

### 1.4 `AgentRunResult` — what an agent returns

```python
@dataclass
class AgentRunResult:
    """What WorkflowRunner.run_agent returns. Wraps the model's structured
    output plus telemetry. The runtime persists this to agent_runs."""
    agent_run_id: str                              # UUID, generated by runner
    agent_name: str
    persona_version: str
    model_used: str                                # the actual model that returned (may be fallback)
    status: Literal["succeeded", "skipped", "failed", "rejected_by_guardrail"]
    output: dict | None                            # the validated tool-call args
    raw_output: dict | None                        # the unvalidated tool-call args (for audit)
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    duration_ms: int
    error: str | None = None
    validation_errors: list[str] = field(default_factory=list)
    llm_audit_log_id: str | None = None            # FK into llm_audit_log
    tool_calls_made: list[dict] = field(default_factory=list)
```

---

## §2 `WorkflowRunner` — the orchestrator

### 2.1 Public interface

```python
# src/agents/runtime/workflow_runner.py
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from uuid import uuid4

from anthropic import AsyncAnthropic, APIError, APIStatusError
from src.agents.runtime.spec import AgentSpec, AgentRunResult, ToolContext, WorkflowSpec
from src.agents.personas import PERSONA_MANIFEST
from src.agents.tools.registry import TOOL_REGISTRY
from src.agents.validators import VALIDATOR_REGISTRY


log = logging.getLogger(__name__)


class WorkflowRunner:
    """Single entry point to execute one WorkflowRun.

    Lifecycle:
        runner = WorkflowRunner(db, redis, anthropic_client, telegram_client)
        result = await runner.run(workflow_spec, params={"capital_base_mode": "deployed"})

    The runner is stateless across runs — instantiate once, call run() per workflow.
    Concurrent runs are safe; each gets its own context.
    """

    def __init__(
        self,
        db_session_factory,
        redis,
        anthropic: AsyncAnthropic,
        telegram=None,
        config: "RunnerConfig | None" = None,
    ):
        self.db_session_factory = db_session_factory
        self.redis = redis
        self.anthropic = anthropic
        self.telegram = telegram
        self.config = config or RunnerConfig()

    async def run(
        self,
        workflow_spec: WorkflowSpec,
        params: dict | None = None,
        triggered_by: str = "scheduled",
        idempotency_key: str | None = None,
    ) -> "WorkflowRunResult":
        """Execute one workflow.

        Args:
            workflow_spec: the WorkflowSpec to execute
            params: runtime parameters merged over workflow_spec.default_params
            triggered_by: "scheduled" | "manual" | "replay" | "shadow_eval"
            idempotency_key: if provided, suppresses duplicate runs in a 5-min
                window (Redis SETNX); used by laabh-runday CLI to make replays safe.

        Returns:
            WorkflowRunResult with status, agent_runs, predictions (if any),
            cost summary, and validator outcomes.

        Raises:
            BudgetExceeded: if cost or token ceilings tripped
            DuplicateRun: if idempotency_key conflicts with an in-flight run
        """
        # ... see implementation in §2.2
```

### 2.2 `run()` implementation outline

```python
    async def run(self, workflow_spec, params=None, triggered_by="scheduled",
                  idempotency_key=None) -> WorkflowRunResult:
        # 1. Idempotency
        if idempotency_key and await self._idempotency_taken(idempotency_key):
            raise DuplicateRun(f"Idempotency key in flight: {idempotency_key}")

        # 2. Parameter resolution
        merged_params = {**workflow_spec.default_params, **(params or {})}

        # 3. Create workflow_run row (status='running')
        workflow_run_id = str(uuid4())
        async with self.db_session_factory() as db:
            await self._create_workflow_run_row(
                db, workflow_run_id, workflow_spec, merged_params, triggered_by
            )
            await db.commit()

        # 4. Build the workflow context
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
            as_of=merged_params.get("as_of") or datetime.now(IST),
        )

        # 5. Execute stages in order
        try:
            for stage in workflow_spec.stages:
                # Check cost circuit breaker BEFORE entering stage
                if ctx.cost_so_far_usd >= workflow_spec.cost_ceiling_usd:
                    raise BudgetExceeded(
                        f"Cost ceiling ${workflow_spec.cost_ceiling_usd} reached "
                        f"before stage {stage.stage_name}; stopping."
                    )
                if ctx.tokens_so_far >= workflow_spec.token_ceiling:
                    raise BudgetExceeded(
                        f"Token ceiling {workflow_spec.token_ceiling} reached"
                    )

                await self._run_stage(stage, ctx)

                # Early exit: brain triage may set skip_today=true
                if self._should_short_circuit(stage, ctx):
                    return await self._finalize_workflow_run(
                        ctx, status="succeeded", short_circuit_reason="brain_skip_today"
                    )

            # 6. Run final cross-agent validators
            validator_outcomes = await self._run_final_validators(workflow_spec, ctx)

            # 7. Persist agent_predictions row(s)
            predictions = await self._persist_predictions(ctx, validator_outcomes)

            # 8. Mark workflow_run succeeded (or succeeded_with_caveats)
            final_status = (
                "succeeded_with_caveats"
                if any(v["outcome"] == "caveat" for v in validator_outcomes)
                else "succeeded"
            )
            return await self._finalize_workflow_run(ctx, status=final_status,
                                                     predictions=predictions,
                                                     validator_outcomes=validator_outcomes)

        except BudgetExceeded as e:
            await self._alert_telegram(f"❌ Workflow {workflow_spec.name} aborted: {e}")
            return await self._finalize_workflow_run(ctx, status="failed", error=str(e))
        except Exception as e:
            log.exception("Workflow run failed unexpectedly")
            await self._alert_telegram(f"❌ Workflow {workflow_spec.name} crashed: {e}")
            return await self._finalize_workflow_run(ctx, status="failed", error=str(e))
```

### 2.3 `_run_stage` — sequential, parallel, conditional

```python
    async def _run_stage(self, stage: WorkflowStage, ctx: WorkflowContext) -> None:
        """Execute one stage. Updates ctx.agent_run_results and ctx.stage_outputs."""

        if stage.kind == "conditional":
            # Evaluate the condition expression against ctx
            if not self._evaluate_condition(stage.condition, ctx):
                log.info(f"Skipping conditional stage {stage.stage_name}")
                return

        # Materialize all StageAgent instances. If iteration_source is set,
        # one StageAgent expands to multiple invocations (one per item).
        invocations = []
        for sa in stage.agents:
            if sa.iteration_source:
                items = self._resolve_iteration(sa.iteration_source, ctx)
                for idx, item in enumerate(items):
                    invocations.append(_AgentInvocation(
                        stage_agent=sa, item=item, item_index=idx,
                    ))
            else:
                invocations.append(_AgentInvocation(stage_agent=sa, item=None, item_index=0))

        if stage.kind == "parallel":
            results = await asyncio.gather(
                *[self._invoke_agent(inv, ctx) for inv in invocations],
                return_exceptions=True,
            )
        else:  # sequential
            results = []
            for inv in invocations:
                results.append(await self._invoke_agent(inv, ctx))

        # Process results: persist successful, log failed, possibly abort
        for inv, res in zip(invocations, results):
            if isinstance(res, Exception):
                log.error(f"Agent {inv.stage_agent.agent_name} raised: {res}")
                if inv.stage_agent.on_iteration_failure == "abort_workflow":
                    raise res
                if inv.stage_agent.on_iteration_failure == "abort_stage":
                    raise StageAborted(stage.stage_name, res)
                # else skip_one: continue
                continue

            ctx.agent_run_results.append(res)
            ctx.cost_so_far_usd += res.cost_usd
            ctx.tokens_so_far += res.input_tokens + res.output_tokens

            # Make output available to downstream stages
            output_key = inv.stage_agent.output_key or inv.stage_agent.agent_name
            if inv.stage_agent.iteration_source:
                ctx.stage_outputs.setdefault(output_key, []).append(res.output)
            else:
                ctx.stage_outputs[output_key] = res.output
```

### 2.4 `_invoke_agent` — the per-call lifecycle

```python
    async def _invoke_agent(
        self, invocation: _AgentInvocation, ctx: WorkflowContext
    ) -> AgentRunResult:
        """The full lifecycle for one agent call: spec lookup, context assembly,
        API call with retries, output validation, cost accounting, audit log."""
        sa = invocation.stage_agent
        agent_run_id = str(uuid4())

        # 1. Resolve the AgentSpec from PERSONA_MANIFEST
        spec = self._build_agent_spec(sa.agent_name, sa.persona_version)

        # 2. Pre-flight token budget check
        prompt_messages = await self._assemble_prompt(spec, invocation, ctx)
        input_tokens_estimate = self._estimate_input_tokens(spec, prompt_messages)
        if input_tokens_estimate > spec.max_input_tokens:
            return AgentRunResult(
                agent_run_id=agent_run_id, agent_name=sa.agent_name,
                persona_version=sa.persona_version, model_used=spec.model,
                status="failed", output=None, raw_output=None,
                cost_usd=Decimal("0"), input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_creation_tokens=0, duration_ms=0,
                error=f"Input estimate {input_tokens_estimate} exceeds spec max {spec.max_input_tokens}",
            )

        # 3. Build the API request body
        api_request = self._build_api_request(spec, prompt_messages, ctx)

        # 4. Persist agent_run row in 'running' state (idempotent)
        await self._persist_agent_run_started(agent_run_id, sa, ctx, input_tokens_estimate)

        # 5. Execute with retry policy
        result = await self._execute_with_retries(
            spec, api_request, agent_run_id, ctx
        )

        # 6. Validate output (forced tool-use already gives shape; Pydantic for semantics)
        if result.status == "succeeded" and spec.output_validator:
            result = await self._validate_output(spec, result, ctx)

        # 7. Persist agent_run row final state
        await self._persist_agent_run_completed(result, ctx)

        return result
```

### 2.5 `_execute_with_retries` — fallback policy

```python
    async def _execute_with_retries(
        self,
        spec: AgentSpec,
        api_request: dict,
        agent_run_id: str,
        ctx: WorkflowContext,
    ) -> AgentRunResult:
        """The retry loop. Two retry budgets: transient errors (network, 5xx,
        overload) and validation errors (the model returned malformed output).

        Transient retry policy:
            attempt 1: primary model
            attempt 2: primary model after exponential backoff
            attempt 3: primary model after longer backoff
            attempt 4 (if fallback_model set and on_failure='degrade'):
                fallback model
            else: return failed AgentRunResult
        """
        transient_attempt = 0
        validation_attempt = 0
        last_error = None
        used_model = spec.model
        api_request_current = api_request

        while transient_attempt <= spec.max_retries_transient:
            try:
                t0 = time.monotonic()
                if spec.stream_response:
                    response, raw = await self._stream_with_progress(
                        spec, api_request_current, agent_run_id, ctx
                    )
                else:
                    response = await asyncio.wait_for(
                        self.anthropic.messages.create(**api_request_current),
                        timeout=spec.timeout_seconds,
                    )
                duration_ms = int((time.monotonic() - t0) * 1000)

                # Extract tool-use block if forced output_tool was set
                tool_call = self._extract_tool_call(response, spec.output_tool)
                if tool_call is None and spec.output_tool is not None:
                    # Model didn't call the tool — treat as validation error
                    raise OutputValidationError(
                        f"Model failed to call required tool {spec.output_tool}"
                    )

                # Cost computation from response.usage
                usage = response.usage
                cost = self._compute_cost(used_model, usage)

                # Persist to llm_audit_log
                audit_id = await self._write_llm_audit_log(
                    spec, used_model, api_request_current, response, ctx, agent_run_id
                )

                return AgentRunResult(
                    agent_run_id=agent_run_id,
                    agent_name=spec.name,
                    persona_version=spec.persona_version,
                    model_used=used_model,
                    status="succeeded",
                    output=tool_call.input if tool_call else self._extract_text(response),
                    raw_output=tool_call.input if tool_call else None,
                    cost_usd=cost,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
                    cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0),
                    duration_ms=duration_ms,
                    llm_audit_log_id=audit_id,
                )

            except OutputValidationError as e:
                # Validation retry: re-prompt with the error attached
                validation_attempt += 1
                last_error = str(e)
                if validation_attempt > spec.max_retries_validation:
                    return self._make_failed_result(
                        spec, agent_run_id, used_model, last_error, "validation_exhausted"
                    )
                api_request_current = self._build_repair_request(
                    api_request_current, response, e
                )

            except (APIStatusError, APIError, asyncio.TimeoutError, ConnectionError) as e:
                transient_attempt += 1
                last_error = f"{type(e).__name__}: {e}"
                log.warning(
                    f"[{spec.name}] transient failure (attempt {transient_attempt}/{spec.max_retries_transient}): {last_error}"
                )
                if transient_attempt > spec.max_retries_transient:
                    # Fallback model path
                    if spec.fallback_model and spec.on_failure == "degrade":
                        log.info(f"[{spec.name}] retrying with fallback model {spec.fallback_model}")
                        used_model = spec.fallback_model
                        api_request_current = {**api_request_current, "model": spec.fallback_model}
                        transient_attempt = 0  # reset budget for fallback
                        continue
                    return self._make_failed_result(
                        spec, agent_run_id, used_model, last_error, "transient_exhausted"
                    )
                await asyncio.sleep(2 ** transient_attempt)  # exponential backoff

        return self._make_failed_result(spec, agent_run_id, used_model, last_error, "loop_exit")
```

### 2.6 `_validate_output` — Pydantic post-validation

```python
    async def _validate_output(
        self, spec: AgentSpec, result: AgentRunResult, ctx: WorkflowContext
    ) -> AgentRunResult:
        """Apply the agent's optional Pydantic validator to the model's output.
        On failure, retry by rebuilding the API request with the validation
        error message embedded; if retries exhausted, mark rejected_by_guardrail.

        Note: the JSON-shape validation already happened (forced tool-use).
        This validator is for SEMANTIC checks like 'capital_pct sums to 100',
        'kill_switch trigger is within ±10% spot', etc.
        """
        validator_cls = VALIDATOR_REGISTRY.get(spec.output_validator)
        if not validator_cls:
            return result

        try:
            validated = validator_cls(**result.output)
            return result  # passed
        except ValidationError as e:
            errors = [str(err) for err in e.errors()]
            log.warning(f"[{spec.name}] output validation failed: {errors}")
            # If we have validation retries left, the runner caller has
            # to handle re-prompting — for now, mark guardrail rejection.
            return AgentRunResult(
                **{**result.__dict__,
                   "status": "rejected_by_guardrail",
                   "validation_errors": errors,
                   "error": f"output_validator={spec.output_validator}: {errors[0]}"}
            )
```

---

## §3 Structured-output enforcement

### 3.1 The forced tool-use pattern

Every agent with an `output_tool` calls the API like:

```python
response = await self.anthropic.messages.create(
    model=spec.model,
    max_tokens=spec.max_output_tokens,
    temperature=spec.temperature,
    system=[
        {
            "type": "text",
            "text": persona_system_prompt,
            "cache_control": {"type": "ephemeral"} if spec.cache_system else None,
        }
    ],
    tools=[
        # The output tool — forced
        OUTPUT_TOOL_SCHEMAS[spec.output_tool],
        # Plus any data-fetching tools
        *[TOOL_REGISTRY[t].json_schema for t in spec.tools],
    ],
    tool_choice={"type": "tool", "name": spec.output_tool},
    messages=user_messages,
)
```

Setting `tool_choice` to a specific tool name guarantees:
- The model's response will contain a `tool_use` block with that tool name
- The block's `input` field will be a dict matching the tool's `input_schema`
- The Anthropic API will reject malformed JSON before returning to us

This eliminates an entire class of "the model forgot a closing brace" errors
that plague free-form JSON generation.

### 3.2 Repair-prompt for validation failures

When the *semantic* validator fails (e.g. `capital_pct` sums to 102, not 100),
the runtime constructs a repair message:

```python
def _build_repair_request(self, original_request: dict, prior_response,
                          validation_error: ValidationError) -> dict:
    """Rebuild the API request to retry with the model's prior output and the
    validation error attached. Capped by spec.max_retries_validation."""
    prior_tool_use = self._extract_tool_use_block(prior_response)
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
                        f"Please re-emit the {original_request['tool_choice']['name']} "
                        f"tool call with corrections. Specifically address the validation "
                        f"errors listed above. Keep all other fields the same unless they "
                        f"depend on the corrected ones."
                    ),
                    "is_error": True,
                }
            ],
        },
    ]
    return {**original_request, "messages": repair_messages}
```

### 3.3 Why not free-form JSON parsing?

Pre-decision rationale, captured here for posterity:

- Forced tool-use uses the *server-side* JSON validator, removing client parser failure modes
- Tool descriptions become the LLM's contract — and our codebase shares the contract
- Tool calls produce structured `usage` accounting (cache hits, etc.) that JSON output blurs
- Repair on tool-use is a *natural* conversation continuation; JSON-string repair is awkward

---

## §4 Sub-agent fan-out helper

The Historical Explorer has 4 parallel sub-agents + 1 aggregator. The
fan-out pattern is reusable enough to warrant a helper:

```python
# src/agents/runtime/parallel.py

async def run_subagents_parallel(
    runner: WorkflowRunner,
    subagent_specs: list[StageAgent],
    shared_input: dict,
    ctx: WorkflowContext,
    aggregate_into: str,
) -> dict:
    """Run N agents in parallel against the same input, then return their
    outputs as a dict keyed by agent name. Used by the Historical Explorer
    pod and (potentially) by other multi-perspective agents.

    Failures within the fan-out follow each sub-agent's on_failure policy;
    if any sub-agent's policy is 'abort', the whole fan-out aborts.
    """
    coros = []
    for sa in subagent_specs:
        invocation = _AgentInvocation(stage_agent=sa, item=shared_input, item_index=0)
        coros.append(runner._invoke_agent(invocation, ctx))

    results = await asyncio.gather(*coros, return_exceptions=True)

    aggregated = {}
    for sa, res in zip(subagent_specs, results):
        if isinstance(res, Exception):
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
```

The Aggregator stage in the workflow definition (see §6) consumes
`ctx.stage_outputs["explorer_subagents"]` directly.

---

## §5 Cost circuit breaker and token budget

### 5.1 Pre-flight projection

Before each stage, the runner projects the stage's worst-case cost using the
sum of its agents' `max_input_tokens × input_price + max_output_tokens × output_price`,
multiplied by iteration count if `iteration_source` is set. If the projection
plus current spend would exceed `cost_ceiling_usd`, the runner aborts before
making any API calls.

```python
def _project_stage_cost(self, stage: WorkflowStage, ctx: WorkflowContext) -> Decimal:
    total = Decimal("0")
    for sa in stage.agents:
        spec = self._build_agent_spec(sa.agent_name, sa.persona_version)
        per_call = (
            Decimal(spec.max_input_tokens) / 1_000_000 * MODEL_PRICES[spec.model]["input"] +
            Decimal(spec.max_output_tokens) / 1_000_000 * MODEL_PRICES[spec.model]["output"]
        )
        n = self._estimate_iteration_count(sa, ctx) if sa.iteration_source else 1
        total += per_call * n
    return total
```

Worst-case projection is conservative; with caching and triage filtering,
actual spend is typically 30-50% of projected.

### 5.2 Hard cost ceiling — runtime check

Even with projection, the actual cost may exceed projection (model returns
more tokens than `max_output_tokens` in tool-call mode is impossible, but
re-prompting can stack). After every agent_run, the runner checks:

```python
if ctx.cost_so_far_usd >= workflow_spec.cost_ceiling_usd:
    raise BudgetExceeded(
        f"Cost ${ctx.cost_so_far_usd} reached ceiling "
        f"${workflow_spec.cost_ceiling_usd} after {ctx.agent_run_results[-1].agent_name}"
    )
```

`BudgetExceeded` is caught by `run()` and converted into a `failed` workflow
with a Telegram alert.

### 5.3 Per-agent token enforcement

The token budget per agent is enforced **before** the API call by
`_estimate_input_tokens`. If estimate exceeds `spec.max_input_tokens`, the
agent fails fast without consuming an API call.

The estimate uses Anthropic's tokenizer where available; fallback is
`len(text) / 3.5` which over-estimates and so is conservative.

### 5.4 Model pricing table (reference)

```python
# src/agents/runtime/pricing.py
from decimal import Decimal

# USD per 1M tokens, input / output
MODEL_PRICES = {
    "claude-haiku-4-5-20251001": {
        "input": Decimal("0.80"),
        "output": Decimal("4.00"),
        "cache_write_input": Decimal("1.00"),
        "cache_read_input": Decimal("0.08"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_write_input": Decimal("3.75"),
        "cache_read_input": Decimal("0.30"),
    },
    "claude-opus-4-7": {
        "input": Decimal("15.00"),
        "output": Decimal("75.00"),
        "cache_write_input": Decimal("18.75"),
        "cache_read_input": Decimal("1.50"),
    },
}
```

These are illustrative — confirm against current Anthropic pricing at deploy
time. The runner should pull from environment-config to allow updates without
code change. All cost computations use `Decimal`, never `float`.

---

## §6 Reference workflow definitions

### 6.1 `predict_today_combined`

```python
# src/agents/workflows/predict_today_combined.py

PREDICT_TODAY_COMBINED_V1 = WorkflowSpec(
    name="predict_today_combined",
    version="v1",
    cost_ceiling_usd=Decimal("5.0"),
    token_ceiling=150_000,
    final_validators=("CEOJudgeOutputValidated",),
    default_params={
        "capital_base_mode": "deployed",
        "deployment_ratio": 0.30,
        "max_drawdown_tolerance_pct": 3.0,
        "target_daily_book_pnl_pct": 10.0,
    },
    stages=(
        WorkflowStage(
            stage_name="brain_triage",
            kind="sequential",
            agents=(
                StageAgent(
                    agent_name="brain_triage",
                    persona_version="v1",
                    output_key="triage",
                ),
            ),
        ),
        # SHORT-CIRCUIT: if triage.skip_today, _should_short_circuit returns True
        # and the workflow ends here.
        WorkflowStage(
            stage_name="news_finder_per_candidate",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="news_finder",
                    persona_version="v1",
                    iteration_source="triage.fno_candidates+triage.equity_candidates",
                    on_iteration_failure="skip_one",
                    output_key="news_findings",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="news_editor_per_finding",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="news_editor",
                    persona_version="v1",
                    iteration_source="news_findings",
                    on_iteration_failure="skip_one",
                    output_key="editor_verdicts",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="explorer_pod_per_candidate",
            kind="parallel",  # iteration over candidates
            agents=(
                StageAgent(
                    agent_name="_explorer_pod",  # pseudo-agent: fan-out helper
                    persona_version="v1",
                    iteration_source="triage.fno_candidates+triage.equity_candidates",
                    on_iteration_failure="skip_one",
                    output_key="explorer_aggregates",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="experts_parallel",
            kind="parallel",
            agents=(
                StageAgent(
                    agent_name="fno_expert",
                    persona_version="v1",
                    iteration_source="triage.fno_candidates",
                    output_key="fno_candidates_full",
                ),
                StageAgent(
                    agent_name="equity_expert",
                    persona_version="v1",
                    iteration_source="triage.equity_candidates",
                    output_key="equity_candidates_full",
                ),
            ),
        ),
        WorkflowStage(
            stage_name="ceo_debate",
            kind="parallel",  # bull and bear in parallel; same data packet, cached
            agents=(
                StageAgent(agent_name="ceo_bull", output_key="bull_brief"),
                StageAgent(agent_name="ceo_bear", output_key="bear_brief"),
            ),
        ),
        WorkflowStage(
            stage_name="ceo_judge",
            kind="sequential",
            agents=(StageAgent(agent_name="ceo_judge", output_key="judge_verdict"),),
        ),
    ),
)
```

The `_explorer_pod` pseudo-agent is the fan-out helper from §4: it expands into
4 parallel sub-agents (trend, past_predictions, sentiment_drift, fno_positioning)
plus 1 aggregator, run as a unit per candidate. Implementation:

```python
async def _run_explorer_pod_per_candidate(self, candidate: dict, ctx) -> dict:
    """Run the 4 sub-agents in parallel, then run the aggregator on their outputs."""
    subagent_specs = [
        StageAgent(agent_name="explorer_trend", persona_version="v1"),
        StageAgent(agent_name="explorer_past_predictions", persona_version="v1"),
        StageAgent(agent_name="explorer_sentiment_drift", persona_version="v1"),
        # F&O positioning only for F&O candidates
        *([StageAgent(agent_name="explorer_fno_positioning", persona_version="v1")]
          if candidate.get("is_fno") else []),
    ]
    sub_outputs = await run_subagents_parallel(
        self, subagent_specs, candidate, ctx, aggregate_into=f"_explorer_{candidate['symbol']}"
    )
    aggregator_input = {"sub_outputs": sub_outputs, "candidate": candidate}
    aggregator_invocation = _AgentInvocation(
        stage_agent=StageAgent(agent_name="explorer_aggregator", persona_version="v1"),
        item=aggregator_input, item_index=0,
    )
    aggregator_result = await self._invoke_agent(aggregator_invocation, ctx)
    return aggregator_result.output
```

### 6.2 Other workflows (specs only — same shape)

```python
PREDICT_TODAY_FNO_V1 = WorkflowSpec(...)        # Same as combined but skips equity stages
PREDICT_TODAY_EQUITY_V1 = WorkflowSpec(...)     # Skips F&O stages
ANALYSE_ONE_INSTRUMENT_V1 = WorkflowSpec(...)   # Single-instrument deep dive, no Brain
EVALUATE_YESTERDAY_V1 = WorkflowSpec(...)       # Resolves yesterday's predictions vs realised data
WEEKLY_POSTMORTEM_V1 = WorkflowSpec(...)        # Calls weekly_postmortem.py — see eval doc
```

`evaluate_yesterday` is structurally simple: one stage running an outcome-resolver
agent that pulls each unresolved `agent_predictions` row from the prior day,
joins to today's actuals (closing prices, hits, P&L), and writes
`agent_predictions_outcomes` rows. It then triggers the shadow_evaluator
on each resolved prediction's parent workflow_run.

---

## §7 Replay semantics

### 7.1 The decision (recap from change-set #1)

**No in-flight resume.** A workflow_run that crashed at agent N must be either:
- Replayed from agent 0 (default), OR
- Replayed starting from a specific completed agent_run (operator-specified)

Replay reads every prior agent_run's input/output from `llm_audit_log` so it
sees the SAME inputs the original run saw — no risk of stale market data
contamination.

### 7.2 The replay flow

```python
async def replay_workflow_run(
    runner: WorkflowRunner,
    original_workflow_run_id: str,
    from_agent: str | None = None,
    persona_version_override: dict[str, str] | None = None,
) -> WorkflowRunResult:
    """Re-execute a prior workflow_run, optionally with persona-version
    overrides for A/B testing (consumed by eval change-set #4).

    from_agent: if set, agent_runs from completed prior steps are replayed
        from llm_audit_log without making fresh API calls; only agents AT
        OR AFTER from_agent make new API calls.
    persona_version_override: e.g. {"fno_expert": "v2"} runs the V2 prompt
        instead of whatever the original run used. Other agents stay on
        their original versions for clean A/B comparison.
    """
    # 1. Fetch the original workflow_run + its agent_runs
    original = await fetch_workflow_run_with_agents(original_workflow_run_id)

    # 2. Build a modified WorkflowSpec with overrides
    workflow_spec = build_replay_spec(original.spec, persona_version_override)

    # 3. Mark the new run as triggered_by="replay" with parent_run_id linkage
    new_run_id = await runner.run(
        workflow_spec,
        params=original.params,
        triggered_by="replay",
        idempotency_key=f"replay-{original_workflow_run_id}-{from_agent or 'start'}",
    )

    # 4. The runner internally detects triggered_by="replay" and, for any
    #    agent that's NOT being overridden AND whose persona_version matches
    #    the original, serves the output from llm_audit_log instead of calling
    #    the API.
    return new_run_id
```

The replay-from-cache path is implemented inside `_invoke_agent`:

```python
    if ctx.is_replay and not self._is_overridden(spec, ctx):
        cached = await self._fetch_from_audit_log(spec, ctx, invocation)
        if cached:
            return self._materialize_result_from_audit(cached, spec)
    # ... else proceed with live API call
```

### 7.3 Orphan reconciliation

```python
# Run at runtime startup and periodically.
async def reconcile_orphan_runs(db_session_factory) -> int:
    """Mark any workflow_run with status='running' and started_at older than
    1 hour as 'orphaned'. Returns count marked.
    """
    async with db_session_factory() as db:
        orphans = await db.execute(
            text("""
                UPDATE workflow_runs
                SET status='failed',
                    status_extended='orphaned',
                    completed_at=NOW(),
                    error='Reconciled as orphan: no progress for >1h'
                WHERE status='running'
                  AND started_at < NOW() - INTERVAL '1 hour'
                RETURNING id
            """)
        )
        await db.commit()
        return orphans.rowcount
```

---

## §8 Atomic `agent_runs` writer

### 8.1 The write contract

Every agent invocation produces exactly one `agent_runs` row. The row is:
- Inserted with status='running' BEFORE the API call (so we can audit even crashes)
- Updated to status='succeeded'/'failed'/'rejected_by_guardrail' AFTER the call returns
- Linked to `llm_audit_log.id` for replay consumption

```python
async def _persist_agent_run_started(
    self, agent_run_id: str, sa: StageAgent, ctx: WorkflowContext,
    estimated_input_tokens: int,
) -> None:
    async with self.db_session_factory() as db:
        await db.execute(text("""
            INSERT INTO agent_runs (
                id, workflow_run_id, agent_name, persona_version, model,
                status, started_at, estimated_input_tokens, iteration_index
            ) VALUES (
                :id, :wfr, :name, :version, :model,
                'running', NOW(), :est, :idx
            )
        """), {
            "id": agent_run_id, "wfr": ctx.workflow_run_id,
            "name": sa.agent_name, "version": sa.persona_version,
            "model": "tbd",  # final model written on completion
            "est": estimated_input_tokens, "idx": 0,
        })
        await db.commit()


async def _persist_agent_run_completed(
    self, result: AgentRunResult, ctx: WorkflowContext
) -> None:
    async with self.db_session_factory() as db:
        await db.execute(text("""
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
        """), {
            "id": result.agent_run_id, "status": result.status,
            "model": result.model_used,
            "output": json.dumps(result.output) if result.output else None,
            "cost": result.cost_usd, "in_tok": result.input_tokens,
            "out_tok": result.output_tokens,
            "cache_r": result.cache_read_tokens, "cache_c": result.cache_creation_tokens,
            "dur": result.duration_ms, "error": result.error,
            "validation_errors": json.dumps(result.validation_errors),
            "audit_id": result.llm_audit_log_id,
        })
        await db.commit()
```

### 8.2 `agent_runs` schema (recap with runtime additions)

```sql
CREATE TABLE IF NOT EXISTS agent_runs (
    id                       UUID PRIMARY KEY,
    workflow_run_id          UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    agent_name               TEXT NOT NULL,
    persona_version          TEXT NOT NULL,
    model                    TEXT NOT NULL DEFAULT 'unknown',  -- the configured model
    model_used               TEXT,                              -- the actual model (may be fallback)
    status                   TEXT NOT NULL,
        CHECK (status IN ('running','succeeded','skipped','failed','rejected_by_guardrail')),
    output                   JSONB,
    raw_output               JSONB,
    cost_usd                 NUMERIC(10,6),
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    cache_read_tokens        INTEGER DEFAULT 0,
    cache_creation_tokens    INTEGER DEFAULT 0,
    duration_ms              INTEGER,
    error                    TEXT,
    validation_errors        JSONB DEFAULT '[]'::jsonb,
    llm_audit_log_id         UUID REFERENCES llm_audit_log(id),
    iteration_index          INTEGER DEFAULT 0,
    estimated_input_tokens   INTEGER,
    started_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at             TIMESTAMPTZ
);

CREATE INDEX agent_runs_workflow_run_id_idx ON agent_runs(workflow_run_id);
CREATE INDEX agent_runs_agent_name_status_idx ON agent_runs(agent_name, status);
CREATE INDEX agent_runs_started_at_idx ON agent_runs(started_at);
```

---

## §9 `llm_audit_log` integration

The runtime extends the existing `llm_audit_log` (already used by
`phase1.extractor` and `fno.thesis`) with new caller-tag values for each
agent. No schema change — only new tag values:

```
caller_tags now used:
  phase1.extractor                 (existing)
  fno.thesis                       (existing)
  agent.brain_triage               (new)
  agent.news_finder                (new)
  agent.news_editor                (new)
  agent.explorer_trend             (new)
  agent.explorer_past_predictions  (new)
  agent.explorer_sentiment_drift   (new)
  agent.explorer_fno_positioning   (new)
  agent.explorer_aggregator        (new)
  agent.fno_expert                 (new)
  agent.equity_expert              (new)
  agent.ceo_bull                   (new)
  agent.ceo_bear                   (new)
  agent.ceo_judge                  (new)
  agent.shadow_evaluator           (new — used by eval change-set #4)
```

The runner writes one `llm_audit_log` row per API call (NOT per repair-retry —
only the final call before success or failure is logged), with:
- `caller_tag`: `agent.{agent_name}`
- `caller_meta`: `{"workflow_run_id": ..., "agent_run_id": ..., "persona_version": ..., "iteration_index": ...}`
- `request`: the full API request body (system + messages + tools + tool_choice)
- `response`: the full API response body
- `cost_usd`, `input_tokens`, `output_tokens`, `cache_*_tokens`

Storing the FULL request/response is what makes replay deterministic.

---

## §10 Cross-agent validators integration

### 10.1 `VALIDATOR_REGISTRY`

```python
# src/agents/validators.py

from typing import Type
from pydantic import BaseModel

class CEOJudgeOutputValidated(BaseModel):
    # see change-set #2 §17 for the full implementation
    ...

VALIDATOR_REGISTRY: dict[str, Type[BaseModel]] = {
    "CEOJudgeOutputValidated": CEOJudgeOutputValidated,
    # Future validators: EquityExpertOutputValidated, FNOExpertOutputValidated, etc.
}
```

### 10.2 `_run_final_validators`

```python
    async def _run_final_validators(
        self, workflow_spec: WorkflowSpec, ctx: WorkflowContext
    ) -> list[dict]:
        """Run cross-agent validators on the final agent_predictions row(s)
        before commit. Each validator runs independently — failures don't
        cascade. Outcomes are recorded for audit; behavior depends on
        guardrail_status."""
        outcomes = []
        # The final row is constructed from ctx.stage_outputs["judge_verdict"]
        candidate_prediction = self._compose_prediction_from_judge(ctx)

        for validator_name in workflow_spec.final_validators:
            validator_cls = VALIDATOR_REGISTRY.get(validator_name)
            if not validator_cls:
                outcomes.append({
                    "validator": validator_name, "outcome": "missing",
                    "error": "Validator not in registry"
                })
                continue

            try:
                validator_cls(**candidate_prediction)
                outcomes.append({"validator": validator_name, "outcome": "passed"})
            except ValidationError as e:
                first_error = e.errors()[0]
                # Decision rule: hard validators (capital_pct sums, at-risk cap)
                # fail the prediction; soft validators (kill_switch realism) just caveat.
                severity = self._validator_severity(validator_name, first_error)
                outcomes.append({
                    "validator": validator_name,
                    "outcome": "rejected" if severity == "hard" else "caveat",
                    "error": str(first_error),
                })
        return outcomes
```

### 10.3 `_persist_predictions`

```python
    async def _persist_predictions(
        self, ctx: WorkflowContext, validator_outcomes: list[dict]
    ) -> list[dict]:
        """Write the final agent_predictions row, with guardrail_status reflecting
        the validator outcomes. Rejected predictions are still persisted (for
        eval and learning) but with status flagged."""
        any_rejected = any(o["outcome"] == "rejected" for o in validator_outcomes)
        any_caveat = any(o["outcome"] == "caveat" for o in validator_outcomes)
        guardrail_status = (
            f"rejected:{validator_outcomes[0]['validator']}" if any_rejected
            else f"caveat:{validator_outcomes[0]['validator']}" if any_caveat
            else "passed"
        )

        prediction = self._compose_prediction_from_judge(ctx)
        prediction_id = str(uuid4())

        async with self.db_session_factory() as db:
            await db.execute(text("""
                INSERT INTO agent_predictions (
                    id, workflow_run_id, as_of, asset_class, symbol_or_underlying,
                    decision, rationale, conviction, expected_pnl_pct, max_loss_pct,
                    target_price, stop_price, horizon, model_used, prompt_versions,
                    guardrail_status, created_at
                ) VALUES (
                    :id, :wfr, :as_of, :ac, :sym,
                    :dec, :rat, :con, :exp, :max,
                    :tgt, :stp, :hor, :mdl, :pv,
                    :gs, NOW()
                )
            """), {
                "id": prediction_id, "wfr": ctx.workflow_run_id,
                "as_of": ctx.as_of, "ac": prediction["asset_class"],
                "sym": prediction["symbol_or_underlying"],
                "dec": prediction["decision"], "rat": prediction["rationale"],
                "con": prediction["conviction"], "exp": prediction["expected_pnl_pct"],
                "max": prediction["max_loss_pct"], "tgt": prediction.get("target_price"),
                "stp": prediction.get("stop_price"), "hor": prediction.get("horizon"),
                "mdl": prediction["model_used"],
                "pv": json.dumps(prediction["prompt_versions"]),
                "gs": guardrail_status,
            })
            await db.commit()
        return [{"id": prediction_id, **prediction, "guardrail_status": guardrail_status}]
```

---

## §11 `RunnerConfig`

```python
@dataclass
class RunnerConfig:
    """Tunable runtime config — overridable via env or laabh-runday CLI."""
    default_temperature: float = 0.0
    default_timeout_seconds: int = 60
    opus_timeout_seconds: int = 180
    transient_retry_max_backoff_s: int = 30
    enable_streaming: bool = True
    enable_caching: bool = True
    telegram_alert_on_failure: bool = True
    telegram_alert_on_caveat: bool = True
    cost_alert_threshold_usd: Decimal = Decimal("3.0")  # alert before hitting ceiling
    orphan_reconciliation_interval_minutes: int = 30
    replay_serve_from_cache_default: bool = True
```

---

## §12 Telegram alerting

The runner integrates with the existing `src/notifications/telegram.py`:

```python
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
```

Alert events:
- Workflow `failed` (any cause)
- `BudgetExceeded` (cost or token ceiling)
- `succeeded_with_caveats` (any guardrail caveat)
- Pre-flight projection > `cost_alert_threshold_usd` (warning before run)
- Orphan reconciliation marked >0 runs (data hygiene)

---

## §13 `laabh-runday` CLI integration

The runner is consumed by the `laabh-runday` CLI (held change-set, not in this
document). The CLI command surface that uses the runner:

```bash
laabh-runday preflight                   # → projects costs, validates configs
laabh-runday run predict_today_combined  # → WorkflowRunner.run(...)
laabh-runday status [<workflow_run_id>]  # → reads workflow_runs + agent_runs
laabh-runday replay <workflow_run_id> [--from-agent <name>]
                                         # → replay_workflow_run(...)
laabh-runday kill-switch                 # → emergency: sets a Redis flag the
                                         #   runner checks before each agent
laabh-runday eod-report                  # → triggers evaluate_yesterday workflow
```

The runner exposes these affordances via simple module-level functions:

```python
# src/agents/runtime/__init__.py
from src.agents.runtime.workflow_runner import WorkflowRunner
from src.agents.runtime.replay import replay_workflow_run
from src.agents.runtime.health import (
    reconcile_orphan_runs, check_kill_switch, project_workflow_cost,
)

__all__ = [
    "WorkflowRunner", "replay_workflow_run", "reconcile_orphan_runs",
    "check_kill_switch", "project_workflow_cost",
]
```

The kill-switch:

```python
async def check_kill_switch(redis) -> bool:
    return await redis.get("laabh:kill_switch") == b"1"

# In WorkflowRunner._run_stage:
if await check_kill_switch(self.redis):
    raise KillSwitchActivated("Operator-initiated kill switch")
```

---

## §14 How to add a new agent

This section is documentation-as-code so the project can extend without
re-deriving the contract.

```
1. Define the system prompt in src/agents/personas/<agent_name>.py
   - Use the eight-component template from change-set #2 §0.1
   - Include {INDIAN_MARKET_DOMAIN_RULES} where domain rules apply
2. Define the output_tool JSON schema in the same file
3. Define any new data-fetching tools in src/agents/tools/<domain>.py
   and register them via TOOL_REGISTRY
4. Add an entry to PERSONA_MANIFEST in src/agents/personas/__init__.py
5. (Optional) Define a Pydantic output validator in src/agents/validators.py
   and register in VALIDATOR_REGISTRY
6. Reference the agent in a WorkflowSpec's stages
7. Write a unit test in tests/agents/test_<agent_name>.py:
   - Mock the API response with a valid tool_use block
   - Assert the runner persists agent_runs correctly
   - Assert the output passes the Pydantic validator
8. Write an eval seed in tests/eval/seeds/<agent_name>.json (consumed by
   change-set #4)
```

The runtime makes ZERO code changes to add a new agent. Adding `news_finder_v2`
is just `PERSONA_MANIFEST["news_finder"]["v2"] = {...}` and referencing it
by `persona_version="v2"` in the workflow.

---

## §15 Implementation checklist

Sequenced for the first PR:

```
1. src/agents/runtime/spec.py                         (~150 lines)
2. src/agents/runtime/workflow_runner.py              (~600 lines)
3. src/agents/runtime/parallel.py                     (~80 lines)
4. src/agents/runtime/replay.py                       (~150 lines)
5. src/agents/runtime/health.py                       (~80 lines)
6. src/agents/runtime/pricing.py                      (~30 lines)
7. src/agents/personas/__init__.py + PERSONA_MANIFEST (~60 lines)
8. src/agents/personas/<13 persona modules>           (~80 lines each, mostly
                                                       importing prompts from
                                                       change-set #2)
9. src/agents/validators.py                           (~200 lines)
10. src/agents/workflows/__init__.py + WorkflowSpec instances (~250 lines)
11. db/migrations/0XX_agent_runs_workflow_runs.sql    (~150 lines)
12. tests/agents/test_workflow_runner.py              (~400 lines)
13. tests/agents/test_replay.py                       (~150 lines)
14. tests/agents/test_validators.py                   (~150 lines)

Total: ~3,500 lines of production code, ~700 lines of tests. One PR per
section — sections 1-6 in PR-1, 7-10 in PR-2, 11 in its own migration PR,
12-14 in PR-3.
```

---

*End of runtime spec. The eval change-set (#4) consumes this runtime as its
substrate — it builds replay tooling and the shadow-evaluator workflow on top
of WorkflowRunner.*
