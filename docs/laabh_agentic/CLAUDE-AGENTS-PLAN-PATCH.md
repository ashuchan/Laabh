# CLAUDE-AGENTS-PLAN-PATCH.md — Surgical Patches to the Agentic Workflow Plan

**Audience:** ashusaxe007@gmail.com
**Date:** 2026-05-07
**Status:** Patch to the agentic workflow architecture plan (1st of 4 change-sets)
**Scope:** Closes the design gaps surfaced in review. No code, no rewrites — only
the targeted fixes that unblock the prompts/runtime/eval change-sets that follow.

This document is meant to be applied **on top of** the existing agentic workflow
plan. Each section is labelled with the section it patches in the parent plan.

---

## Patch §3.3 — Historical Explorer Aggregator (NEW persona)

The original plan listed `tradable_pattern_score`, `signals_to_watch`, and
`do_not_repeat` as fields of the aggregator's output but did not specify *who*
produces them. Python concatenation cannot generate interpretive fields.

**Resolution:** add a 5th sub-agent that runs *after* the four parallel sub-agents.
It is the only agent in the Explorer pod with cross-cutting context.

| Property | Value |
|---|---|
| Persona ID | `historical_explorer_aggregator` |
| Model | Sonnet 4.6 (interpretive, but cheap because input is already pre-summarised) |
| Inputs | The four sub-agent outputs (Trend, Past-Prediction, Sentiment-Drift, F&O-Positioning) |
| Tools | None — pure synthesis |
| Cost ceiling | 1 call/instrument/workflow_run |
| Output | `tradable_pattern_score (0..1)`, `signals_to_watch (≤5)`, `do_not_repeat (≤5)`, `dominant_horizon ∈ {1w,15d,1m}`, `regime_consistency_with_today (low|med|high)`, `tldr (≤80 tokens for CEO consumption)` |

The aggregator persona prompt is in `CLAUDE-AGENTS-PROMPTS-AND-TOOLS.md` §6.

---

## Patch §3.4 — Brain Triage call (FULL spec)

The original plan stubbed this as "a cheap Haiku triage call over the watchlist
+ today's top movers." For a call that decides which symbols receive expensive
downstream attention, that is under-specified. Brain triage gets the full eight-
component treatment.

### 3.4.1 Inputs (Python-assembled, fed to Haiku as a structured user message)

The Brain assembles the following packet **before** calling the LLM:

```python
@dataclass
class BrainTriageContext:
    as_of: datetime                            # IST, 09:00 by default
    market_regime: dict                        # from regime_gate.py: {vix, vix_regime, nifty_trend_1d, nifty_trend_5d}
    universe: list[InstrumentSummary]          # all F&O-eligible + watchlist equity, ban_list filtered
    signal_velocity: dict[int, SignalVelocity] # per-instrument: signals_24h, bullish/bearish split, top analyst credibility
    yesterday_outcomes: list[OutcomeSummary]   # yesterday's predictions resolved + their P&L
    open_positions: list[PositionSummary]      # so we don't repeat-recommend
    top_movers: list[MoverSummary]             # gainers/losers >2% pre-market
    today_calendar: dict                       # results, ex-dates, FOMC, RBI events
    cost_budget_remaining_usd: Decimal         # workflow's own budget signal
```

`InstrumentSummary` includes only what's needed for triage — not full chain data.
The point is to keep this packet under 12k tokens so the Haiku call is fast and
cheap.

### 3.4.2 Output schema

```json
{
  "as_of": "2026-05-07T09:00:00+05:30",
  "skip_today": false,
  "skip_reason": null,
  "fno_candidates": [
    {"underlying_id": 12, "symbol": "BANKNIFTY",
     "rank_score": 0.86, "primary_driver": "rate-cut chatter + IV cheap",
     "watch_for": "RBI commentary tail risk", "expected_strategy_family": "directional_long"}
  ],
  "equity_candidates": [
    {"instrument_id": 234, "symbol": "TATAMOTORS",
     "rank_score": 0.78, "primary_driver": "JLR Q4 beat, bullish convergence",
     "watch_for": "Auto sector breadth", "horizon_hint": "5d"}
  ],
  "explicit_skips": [
    {"symbol": "IRCTC", "reason": "in F&O ban list"},
    {"symbol": "RELIANCE", "reason": "already 35% allocated, no fresh add"}
  ],
  "regime_note": "VIX 18.4 — regime upper-edge, prefer spreads over naked options",
  "estimated_downstream_calls": {"fno_expert": 5, "equity_expert": 5}
}
```

### 3.4.3 Hard rules (enforced in Python after the LLM returns)

- `len(fno_candidates) + len(equity_candidates) ≤ MAX_TRIAGE_OUTPUT` (default 10).
- Every candidate `symbol` must exist in `universe` (no hallucination).
- No candidate may also appear in `open_positions` unless explicitly flagged as
  `add_to_position: true`.
- If `skip_today: true`, the workflow short-circuits — no downstream agents run.
  This is the cheapest abort path in the system; the prompt must affirmatively
  reward using it (see calibration in the prompt doc).

The full Brain triage prompt is in `CLAUDE-AGENTS-PROMPTS-AND-TOOLS.md` §2.

---

## Patch §3.7 — CEO debate redesign

The original plan said "Pass A — Bull/Bear sparring; Pass B — CEO verdict" with
no internal structure. The redesign:

### 3.7.1 Three Opus calls, not two

| Call | Persona | Cached? | Purpose |
|---|---|---|---|
| `ceo_bull` | Bullish PM | data block cached | Build the strongest case FOR maximal deployment |
| `ceo_bear` | Bearish PM | data block cached (same cache key) | Build the strongest case FOR caution / cash |
| `ceo_judge` | Strategist | bull + bear briefs | Produce the final allocation row |

Both `ceo_bull` and `ceo_bear` consume the *exact same* data packet: ranked F&O
candidates, ranked equity candidates, portfolio snapshot, India VIX, NIFTY
regime, the editor verdicts, and the explorer aggregator outputs. Use **prompt
caching** on this packet — the personas differ only in their system prompt.

This adds one Opus call vs the original plan but the data packet is cached,
so net cost increase is ~25%, not 50%. Worth it for the structured disagreement.

### 3.7.2 Symmetric structured-argument schema (Bull and Bear)

Both sides return the same shape:

```json
{
  "stance": "bullish_aggressive | bullish_measured | bearish_measured | bearish_defensive",
  "core_thesis": "<2-sentence headline argument>",
  "top_3_evidence": [
    {"claim": "...", "evidence_type": "signal|filing|technical|macro|positioning",
     "provenance": {"signal_id": "uuid"|null, "raw_content_id": 4456|null,
                    "metric": "PCR=1.08"|null},
     "weight": 0.0-1.0}
  ],
  "top_3_counter_to_other_side": [
    {"likely_other_side_claim": "...",
     "rebuttal": "...",
     "rebuttal_strength": "weak|medium|strong"}
  ],
  "preferred_allocation": [
    {"asset_class": "fno|equity|cash", "underlying_or_symbol": "...", "capital_pct": 35}
  ],
  "conviction": 0.0-1.0,
  "what_would_change_my_mind": [
    "<3-5 specific market events or data prints that would invalidate this view>"
  ]
}
```

The `what_would_change_my_mind` field is operationally critical: the Judge uses
it to construct the `kill_switch` text in the final verdict, so the kill-switch
isn't ad-hoc but tied to a specific surviving counter-argument.

### 3.7.3 Judge call — schema and behaviour

```json
{
  "decision_summary": "<3 sentences, written for the morning brief>",
  "disagreement_loci": [
    {"topic": "<e.g. BANKNIFTY direction>", "bull_view": "...", "bear_view": "...",
     "judge_lean": "bull|bear|split", "lean_strength": "weak|medium|strong",
     "decisive_evidence": "<which side's evidence weighed more, why>"}
  ],
  "allocation": [...],   // same shape as original plan's predictions output
  "expected_book_pnl_pct": 6.2,
  "stretch_pnl_pct": 11.0,
  "max_drawdown_tolerated_pct": 3.0,
  "kill_switches": [
    {"trigger": "<from bear's what_would_change_my_mind>",
     "action": "exit_all | scale_down_50 | tighten_stops",
     "monitoring_metric": "<concrete metric and threshold>"}
  ],
  "ceo_note": "<5-sentence narrative for human reader>",
  "calibration_self_check": {
    "bullish_argument_grade": "A|B|C|D",
    "bearish_argument_grade": "A|B|C|D",
    "confidence_in_allocation": 0.0-1.0,
    "regret_scenario": "<one-line: which way is the regret asymmetric?>"
  }
}
```

The `calibration_self_check` is the Judge auditing itself. Without it, the Judge
tends to land at 60/40 with no real conviction; forcing it to grade both
arguments and articulate the asymmetric regret produces sharper allocations.

The full CEO prompts (Bull, Bear, Judge) are in `CLAUDE-AGENTS-PROMPTS-AND-TOOLS.md` §11–13.

---

## Patch §9 — Capital base (CLOSED, not open)

The plan listed this as an open question. It is not — without a default, the CEO
prompt has no anchor for what "10% profit" means. Decision:

> **Default**: 10% of *deployed capital per workflow_run*, where deployed capital
> = total portfolio value × `capital_deployment_ratio` (default 0.30, i.e.
> 30% deployable per day across all open positions).
>
> **Hard cap**: max 3% of *total portfolio value* at risk per day. The CEO's
> allocation must respect this; cross-agent guardrail (§ below) enforces it.
>
> **Override**: workflow `params.capital_base_mode ∈ {deployed, total, custom}`
> with optional `params.custom_capital_base_inr` for one-off backtests.

This is a sensible default for a personal-use paper-trading system. User can flip
modes per-run via `WorkflowRunner.run("predict_today_combined", params={"capital_base_mode": "total"})`.

---

## Cross-agent consistency validators (NEW, post-CEO)

These are Pydantic validators that run *after* the Judge returns and *before* the
`agent_predictions` row is committed. A failure routes to a `rejected_by_guardrail`
agent_run and the workflow ends in `succeeded_with_caveats` status (new enum
value, see schema patch below).

```python
class CEOOutputValidator(BaseModel):
    allocation: list[Allocation]

    @validator("allocation")
    def capital_pct_sums_to_at_most_100(cls, v):
        total = sum(a.capital_pct for a in v)
        if total > Decimal("100.01"):
            raise ValueError(f"Allocation sums to {total}, must be ≤100")
        return v

    @validator("allocation")
    def at_risk_under_3pct(cls, v, values):
        # max_loss_pct of each non-cash leg × capital_pct, summed
        # must be ≤ 3% of total portfolio
        ...

    @validator("allocation")
    def no_overlap_unless_hedge(cls, v):
        # If RELIANCE appears in fno (long) and equity (long), reject unless flagged
        ...

    @validator("allocation")
    def fno_legs_match_direction(cls, v):
        # bull_call_spread must have BUY CE @ lower strike + SELL CE @ higher
        ...

    @validator("allocation")
    def kill_switch_within_realistic_range(cls, v):
        # The trigger price in kill_switch must be within ±10% of current spot
        ...

    @validator("allocation")
    def buy_implies_target_above_entry(cls, v):
        # For BUY direction: target > entry > stop
        # For SELL: stop > entry > target
        ...
```

Validators run in order; the **first** failure short-circuits but the agent_run
records *which* validator tripped, so we can track failure modes across runs.

---

## Naming clarification

The parent plan used `predictions` for the new table. Renaming to **`agent_predictions`** to disambiguate from the `signals` table (external inputs), `strategy_decisions` (downstream auto-trader decisions), and the colloquial sense of "predictions" anywhere else in the codebase.

| Concept | Table | Producer | Consumer |
|---|---|---|---|
| External recommendation extracted from news | `signals` | `phase1.extractor` | Convergence engine, Brain |
| Brain's final allocation decision | `agent_predictions` (new) | CEO Judge | Auto-trader, mobile UI |
| Auto-trader's executed paper trade | `strategy_decisions` | Auto-trader | Portfolio P&L |
| Per-agent reasoning step | `agent_runs` (new) | All agents | Eval, replay |

All §5 schema in the parent plan should be updated to use `agent_predictions`
(and `agent_predictions_outcomes` for the outcomes table).

---

## Resumability decision: replay, not resume

The parent plan's `workflow_runs.status` allows for `running`, but didn't say what
happens to in-flight runs after a crash. **Decision: replay-only, no resume.**

Rationale:
- A half-completed `agent_runs` chain that resumes mid-flight risks operating on
  stale market data (a workflow started at 09:00 and resumed at 09:45 sees a
  different universe).
- Replay from a checkpoint is deterministic given the same inputs (which we have
  in `llm_audit_log`).
- Workflow cost is bounded enough (~$1–2 worst case) that re-running is fine.

Implementation:

```python
# At runtime startup
async def reconcile_orphan_runs():
    """Mark any workflow_run with status='running' and started_at < (now - 1h)
    as 'orphaned'. Operator can manually replay from the last completed agent_run."""
    ...

# Operator command
laabh-runday replay-workflow <workflow_run_id> [--from-agent <agent_name>]
```

`agent_runs.created_at` ordering gives us the natural checkpoint sequence.

---

## Token budgets per agent (NEW, enforced in WorkflowRunner)

| Agent | Input cap (tokens) | Output cap (tokens) | Rationale |
|---|---|---|---|
| Brain triage | 12,000 | 1,500 | Big universe, terse output |
| News Finder | 16,000 | 2,500 | Many articles, narrative output |
| News Editor | 4,000 | 800 | Critique, tight |
| Trend sub-agent | 6,000 | 1,500 | OHLC tables → interpretation |
| Past-Prediction sub-agent | 8,000 | 1,500 | Past rows → lessons |
| Sentiment-Drift sub-agent | 6,000 | 1,200 | Time-series → trend |
| F&O-Positioning sub-agent | 8,000 | 1,500 | Chain snapshot → structure |
| Explorer Aggregator | 6,000 | 1,200 | 4 sub-agent outputs → synthesis |
| F&O Expert | 12,000 | 2,500 | Per-candidate, full thesis |
| Equity Expert | 10,000 | 2,000 | Per-candidate |
| CEO Bull | 18,000 | 3,000 | Full data packet, structured argument |
| CEO Bear | 18,000 | 3,000 | Same |
| CEO Judge | 22,000 | 4,000 | Bull + Bear briefs + portfolio context |
| Shadow Evaluator (eval) | 12,000 | 2,000 | Reads parent's agent_runs |

Workflow-level total ceiling: **150,000 tokens / $5 USD** for `predict_today_combined`.
Cost circuit breaker (in `WorkflowRunner`) aborts if either is breached.

---

## Streaming for Opus calls

CEO Bull / Bear / Judge calls are 30–60 seconds. Stream into `agent_runs.output`
as partial rows so:

- Operator monitoring (the `laabh-runday status` CLI) shows progress.
- Failures mid-stream produce partial-output rows useful for debugging.
- `llm_audit_log.response` is written *after* completion (full transcript), but
  `agent_runs.output` is updated incrementally.

Other agents (Sonnet/Haiku) are <5 seconds — no streaming needed.

---

## Schema patches to parent plan §5

Add three columns to `agent_predictions` and one to `workflow_runs`:

```sql
ALTER TABLE agent_predictions ADD COLUMN model_used TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE agent_predictions ADD COLUMN prompt_versions JSONB NOT NULL DEFAULT '{}'::jsonb;
   -- {"news_finder": "v1", "fno_expert": "v2", "ceo_judge": "v1"}
ALTER TABLE agent_predictions ADD COLUMN guardrail_status TEXT NOT NULL DEFAULT 'passed';
   -- 'passed' | 'caveat:<validator_name>' | 'rejected:<validator_name>'

ALTER TABLE workflow_runs ADD COLUMN status_extended TEXT;
-- For runs that succeeded but with guardrail caveats:
-- 'succeeded' | 'succeeded_with_caveats' | 'failed' | 'cancelled' | 'orphaned'
-- The original `status` column stays for backward compat; status_extended is the
-- richer enum.
```

`prompt_versions` is the single most important new field — it's what enables the
weekly postmortem in change-set #4 to attribute P&L shifts to specific prompt
changes vs market regime changes.

---

## What this patch does NOT change

- The five primitives (Workflow, WorkflowRun, AgentRun, AgentPrediction, AgentPredictionOutcome).
- The agent catalogue at the *structural* level (News Finder → Editor → Explorer → Experts → CEO).
- The reuse audit in §7 (existing prompts, strategies, scoring modules).
- The phased delivery in §8.
- The non-goals in §10.

These remain as-is. The patches above are the minimum surface area needed to
make the prompts/runtime/eval change-sets implementable without further design
work.

---

*End of patch. Apply alongside the original agentic workflow plan; the next
three change-sets reference this patch's decisions as load-bearing.*
