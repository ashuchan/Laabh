# CLAUDE-AGENTS-EVAL.md — Evaluation, Replay, and Continuous Improvement

**Audience:** ashusaxe007@gmail.com
**Date:** 2026-05-07
**Status:** Production eval spec (4th of 4 change-sets)
**Scope:** The two-tier evaluation system — live shadow eval (daily, every
workflow_run gets an in-flight quality audit) plus a weekly notebook (Sunday
postmortem with regression suite, prompt-version A/B, and P&L attribution).
Plus the shared replay harness that underpins both.

This document is the closing piece of the four-change-set drop. It consumes:
- The shadow_evaluator persona from CLAUDE-AGENTS-PROMPTS-AND-TOOLS.md §14
- The `WorkflowRunner` and `replay_workflow_run` from CLAUDE-AGENTS-RUNTIME.md
- The `agent_predictions` and `agent_runs` schemas from CLAUDE-AGENTS-PLAN-PATCH.md

---

## §0 Two-tier eval design

```
┌─────────────────────────────────────────────────────────────────────────┐
│  EVERY WORKFLOW RUN                                                      │
│  predict_today_combined finishes at ~09:30                               │
│         │                                                                │
│         ▼                                                                │
│  shadow_evaluator workflow auto-fires (parallel to market resolution)    │
│         │                                                                │
│         ▼                                                                │
│  agent_predictions_eval row written: {calibration, evidence_alignment,   │
│  guardrail_proximity, novelty, self_consistency} scores 0-10 each        │
│         │                                                                │
│         ▼                                                                │
│  Telegram alert ONLY if any score < 4 OR self_consistency has bugs       │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  EVERY SUNDAY 18:00 IST                                                  │
│  weekly_postmortem.py runs, consuming the past 7 days of:                │
│    • agent_predictions + agent_predictions_outcomes (P&L)                │
│    • agent_predictions_eval (live shadow eval scores)                    │
│    • llm_audit_log (cost trends)                                         │
│    • Optional: replay-A/B run results for prompt iteration               │
│         │                                                                │
│         ▼                                                                │
│  Outputs:                                                                │
│    • reports/weekly/YYYY-WW.md (operator's weekly briefing)              │
│    • Telegram digest (top 5 takeaways, ≤80 tokens each)                  │
│    • prompt_version_results table updated with the week's A/B outcomes   │
└─────────────────────────────────────────────────────────────────────────┘
```

The two tiers are NOT redundant — the daily shadow catches calibration drift
*before* the market resolves, while the weekly notebook does P&L attribution
*after* outcomes are known. They use the same replay harness, which is why
they're delivered together.

---

## §1 The shared replay harness

### 1.1 `scripts/replay_workflow_run.py`

```python
#!/usr/bin/env python3
"""Replay a prior workflow_run, with optional persona-version overrides.

Used by:
  - eval/shadow.py (live shadow eval, indirectly — the shadow eval doesn't
    replay, but the harness reuses the same llm_audit_log readers)
  - scripts/weekly_postmortem.py (A/B testing prompt versions)
  - laabh-runday CLI (operator-driven replay after a crash)

Examples:
  # Faithful replay (same persona versions, serves from cache for unchanged agents)
  python scripts/replay_workflow_run.py 2026-05-06-uuid

  # A/B replay with one prompt swapped
  python scripts/replay_workflow_run.py 2026-05-06-uuid \
      --persona-version fno_expert=v2

  # Replay only from a specific step (for crash recovery)
  python scripts/replay_workflow_run.py 2026-05-06-uuid \
      --from-agent ceo_judge

  # Multi-version replay (compare three prompt variants)
  python scripts/replay_workflow_run.py 2026-05-06-uuid \
      --persona-version news_finder=v2 --tag exp-news-v2
  python scripts/replay_workflow_run.py 2026-05-06-uuid \
      --persona-version news_finder=v3 --tag exp-news-v3
"""

import argparse
import asyncio
import sys
from uuid import UUID

from src.agents.runtime import WorkflowRunner, replay_workflow_run
from src.agents.runtime.spec import WorkflowSpec
from src.agents.workflows import WORKFLOW_REGISTRY
from src.db.session import db_session_factory
from src.notifications.telegram import telegram_client
from anthropic import AsyncAnthropic
from src.config import settings


async def main(args: argparse.Namespace) -> int:
    runner = WorkflowRunner(
        db_session_factory=db_session_factory,
        redis=await get_redis(),
        anthropic=AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY),
        telegram=telegram_client(),
    )

    overrides = {}
    if args.persona_version:
        for entry in args.persona_version:
            agent, version = entry.split("=", 1)
            overrides[agent] = version

    new_run = await replay_workflow_run(
        runner=runner,
        original_workflow_run_id=str(args.workflow_run_id),
        from_agent=args.from_agent,
        persona_version_override=overrides or None,
    )

    print(f"Replay run id: {new_run.workflow_run_id}")
    print(f"Status: {new_run.status}")
    print(f"Cost: ${new_run.cost_usd:.4f}")
    print(f"Predictions: {len(new_run.predictions)}")

    if args.tag:
        await tag_replay(new_run.workflow_run_id, args.tag)

    return 0 if new_run.status in ("succeeded", "succeeded_with_caveats") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow_run_id", type=UUID)
    parser.add_argument(
        "--persona-version", action="append",
        help="Override a persona version, e.g. --persona-version fno_expert=v2. "
             "Pass multiple to override several agents."
    )
    parser.add_argument("--from-agent", type=str, default=None,
        help="Replay starting from this agent name. Earlier agents serve from cache.")
    parser.add_argument("--tag", type=str, default=None,
        help="Optional tag for grouping experimental replays "
             "(stored in workflow_runs.experiment_tag).")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
```

### 1.2 Replay's two modes — faithful vs experimental

**Faithful replay** (no `--persona-version` overrides):
- Serves every agent's input/output from `llm_audit_log` — zero new API calls.
- Used to debug a specific run's logic without re-spending budget.
- Useful after a crash: `replay --from-agent ceo_judge` re-runs only from the
  point of failure, with all prior agent_runs deserialised from the audit log.

**Experimental replay** (with `--persona-version`):
- Specified agents make new API calls with the override version's prompt.
- Other agents serve from cache (so the inputs to the overridden agent are
  byte-identical to the original run — clean A/B isolation).
- Cost: only the overridden agents incur new spend.
- The new `workflow_run` row is tagged with `triggered_by="replay"` and
  `experiment_tag=<args.tag>` for downstream aggregation.

### 1.3 `experiment_tag` and `parent_run_id`

Schema extension for `workflow_runs`:

```sql
ALTER TABLE workflow_runs ADD COLUMN parent_run_id UUID REFERENCES workflow_runs(id);
ALTER TABLE workflow_runs ADD COLUMN experiment_tag TEXT;
ALTER TABLE workflow_runs ADD COLUMN persona_version_overrides JSONB DEFAULT '{}'::jsonb;

CREATE INDEX workflow_runs_parent_run_id_idx ON workflow_runs(parent_run_id);
CREATE INDEX workflow_runs_experiment_tag_idx ON workflow_runs(experiment_tag)
    WHERE experiment_tag IS NOT NULL;
```

Together these enable the weekly postmortem to group replays:
- All experiments tagged `exp-news-v2` → one A/B group
- All replays of a single original run → traceable chain

---

## §2 Live shadow eval (daily, in-flight)

### 2.1 Trigger mechanism

Every successful `predict_today_*` workflow_run automatically triggers a
shadow_evaluator workflow as the FINAL stage. This is built into the workflow
spec, not a separate cron — it runs in the same process, immediately after the
Judge writes the prediction.

```python
# src/agents/workflows/predict_today_combined.py — extended

PREDICT_TODAY_COMBINED_V1 = WorkflowSpec(
    name="predict_today_combined",
    version="v1",
    cost_ceiling_usd=Decimal("5.50"),  # +0.50 for shadow eval
    token_ceiling=165_000,             # +15k for shadow eval
    final_validators=("CEOJudgeOutputValidated",),
    stages=(
        # ... earlier stages from runtime change-set #3 §6.1 ...
        WorkflowStage(
            stage_name="shadow_evaluation",
            kind="sequential",
            agents=(
                StageAgent(
                    agent_name="shadow_evaluator",
                    persona_version="v1",
                    output_key="shadow_eval",
                ),
            ),
        ),
    ),
)
```

The shadow_evaluator is added as the LAST stage so it has access to the full
prior chain via `ctx.agent_run_results` and `ctx.stage_outputs`.

### 2.2 The shadow_evaluator's input

The runner constructs the shadow eval's input from:

```python
shadow_input = {
    "workflow_run_id": ctx.workflow_run_id,
    "workflow_name": workflow_spec.name,
    "as_of": ctx.as_of,
    "params": ctx.params,
    "agent_runs": [
        {
            "agent_name": ar.agent_name,
            "persona_version": ar.persona_version,
            "model": ar.model_used,
            "status": ar.status,
            "input_summary": _truncate_for_eval(ar.inputs, 500),
            "output": ar.output,
            "cost_usd": ar.cost_usd,
        }
        for ar in ctx.agent_run_results
        if ar.agent_name != "shadow_evaluator"  # don't eval ourselves
    ],
    "final_predictions": ctx.stage_outputs.get("predictions_pending_commit", []),
    "recent_history": await _fetch_recent_history(
        workflow_name=workflow_spec.name, n=5, db=ctx.db_session_factory
    ),
}
```

The `input_summary` truncation is critical — sending the full agent_runs[].inputs
would blow the shadow eval's 12k input budget. We send only:
- For agents with structured outputs: full output, truncated input summary
- For news_finder: full themes + summary_json + first 200 chars of narrative
- For experts: full output schema (rationale, conviction, score, tldr)

### 2.3 The `agent_predictions_eval` table

```sql
CREATE TABLE IF NOT EXISTS agent_predictions_eval (
    id                       UUID PRIMARY KEY,
    workflow_run_id          UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    prediction_id            UUID REFERENCES agent_predictions(id),
    evaluator_agent_run_id   UUID NOT NULL REFERENCES agent_runs(id),
    evaluator_persona_version TEXT NOT NULL,

    -- Per-dimension scores (0-10 each, 0 = severe issue, 10 = exemplary)
    calibration_score         NUMERIC(3,1),
    evidence_alignment_score  NUMERIC(3,1),
    guardrail_proximity_score NUMERIC(3,1),
    novelty_score             NUMERIC(3,1),
    self_consistency_score    NUMERIC(3,1),

    -- Computed composite
    overall_score             NUMERIC(3,1) GENERATED ALWAYS AS (
        (calibration_score + evidence_alignment_score + guardrail_proximity_score
         + novelty_score + self_consistency_score) / 5
    ) STORED,

    -- Justifications and flags
    headline_concern          TEXT,
    is_re_skin                BOOLEAN DEFAULT false,
    is_repeat_mistake         BOOLEAN DEFAULT false,
    matched_history_run_ids   JSONB DEFAULT '[]'::jsonb,
    near_misses               JSONB DEFAULT '[]'::jsonb,
    inconsistencies           JSONB DEFAULT '[]'::jsonb,

    -- Provenance
    eval_cost_usd             NUMERIC(10,6),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_predictions_eval_wfr_idx ON agent_predictions_eval(workflow_run_id);
CREATE INDEX agent_predictions_eval_score_idx
    ON agent_predictions_eval(overall_score) WHERE overall_score < 5;
CREATE INDEX agent_predictions_eval_created_at_idx ON agent_predictions_eval(created_at);
```

### 2.4 Persisting shadow eval output

The runner has a special hook for the shadow_evaluator's persistence (it
writes to `agent_predictions_eval` instead of `agent_predictions`):

```python
# src/agents/runtime/post_processors.py

POST_PROCESSORS: dict[str, Callable] = {
    "shadow_evaluator": _persist_shadow_eval_output,
}


async def _persist_shadow_eval_output(
    result: AgentRunResult, ctx: WorkflowContext
) -> None:
    """Persist shadow_evaluator output to agent_predictions_eval, not
    agent_predictions. Called by WorkflowRunner._invoke_agent when agent is
    in POST_PROCESSORS."""
    if result.status != "succeeded":
        return
    output = result.output
    scores = output.get("scores") or {}

    async with ctx.db_session_factory() as db:
        await db.execute(text("""
            INSERT INTO agent_predictions_eval (
                id, workflow_run_id, prediction_id, evaluator_agent_run_id,
                evaluator_persona_version,
                calibration_score, evidence_alignment_score,
                guardrail_proximity_score, novelty_score, self_consistency_score,
                headline_concern, is_re_skin, is_repeat_mistake,
                matched_history_run_ids, near_misses, inconsistencies,
                eval_cost_usd, created_at
            ) VALUES (
                :id, :wfr, :pid, :ear, :pv,
                :cal, :ea, :gp, :nov, :sc,
                :hc, :rs, :rm, :mh, :nm, :inc,
                :cost, NOW()
            )
        """), {
            "id": str(uuid4()),
            "wfr": ctx.workflow_run_id,
            "pid": ctx.stage_outputs.get("prediction_id"),
            "ear": result.agent_run_id,
            "pv": result.persona_version,
            "cal": scores.get("calibration", {}).get("score"),
            "ea":  scores.get("evidence_alignment", {}).get("score"),
            "gp":  scores.get("guardrail_proximity", {}).get("score"),
            "nov": scores.get("novelty", {}).get("score"),
            "sc":  scores.get("self_consistency", {}).get("score"),
            "hc": output.get("headline_concern"),
            "rs": scores.get("novelty", {}).get("is_re_skin", False),
            "rm": scores.get("novelty", {}).get("is_repeat_mistake", False),
            "mh": json.dumps(scores.get("novelty", {}).get("matched_history_run_ids", [])),
            "nm": json.dumps(scores.get("guardrail_proximity", {}).get("near_misses", [])),
            "inc": json.dumps(scores.get("self_consistency", {}).get("inconsistencies", [])),
            "cost": result.cost_usd,
        })
        await db.commit()

    if output.get("alert_operator"):
        await ctx.telegram.send(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=(
                f"⚠️ Shadow eval flagged today's run\n"
                f"Workflow: {ctx.workflow_run_id}\n"
                f"Concern: {output.get('headline_concern')}\n"
                f"Lowest score: {min((s.get('score', 10) for s in scores.values()), default=10)}/10"
            ),
        )
```

### 2.5 Daily alerting thresholds

```python
# In shadow_evaluator's prompt (already in change-set #2 §14):
# alert_operator=true if any score < 4 OR self_consistency.inconsistencies non-empty.

# Additional non-LLM alert in the runner:
async def check_daily_eval_alerts(ctx: WorkflowContext) -> None:
    """Run after persisting shadow eval — additional algorithmic checks beyond
    the LLM evaluator's own alert flag."""
    overall_scores_last_5d = await fetch_overall_scores(days=5)
    if len(overall_scores_last_5d) >= 3:
        rolling_avg = sum(overall_scores_last_5d[-3:]) / 3
        if rolling_avg < 6.0:
            await alert_telegram(
                f"📉 3-day eval avg dropped to {rolling_avg:.1f}/10 — "
                f"inspect prompt drift",
                severity="warning",
            )
```

---

## §3 Weekly postmortem (Sunday 18:00 IST)

### 3.1 `scripts/weekly_postmortem.py`

```python
#!/usr/bin/env python3
"""Weekly Sunday postmortem. Runs at 18:00 IST every Sunday via cron.

Outputs:
  - reports/weekly/2026-W18.md (full report)
  - Telegram digest (top 5 takeaways)
  - prompt_version_results table (week's A/B outcomes)

Usage:
  python scripts/weekly_postmortem.py                    # last 7 days
  python scripts/weekly_postmortem.py --week 2026-W17    # specific week
  python scripts/weekly_postmortem.py --replay-ab        # also kick off A/B replays
"""

import argparse
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

from src.eval.weekly import (
    fetch_week_data,
    compute_pnl_attribution,
    compute_calibration_drift,
    compute_cost_per_correct_prediction,
    run_regression_suite,
    run_prompt_version_ab,
    render_markdown_report,
    send_telegram_digest,
    persist_prompt_version_results,
)


async def main(args) -> int:
    week_iso = args.week or _current_iso_week()
    start, end = _iso_week_to_dates(week_iso)

    # 1. Pull the week's data
    week = await fetch_week_data(start, end)

    # 2. Compute the analytics blocks
    pnl_attribution = await compute_pnl_attribution(week)
    calibration_drift = compute_calibration_drift(week)
    cost_correct = compute_cost_per_correct_prediction(week)
    regression_results = await run_regression_suite()  # always run

    # 3. (Optional) A/B replays of staged prompt versions
    ab_results = []
    if args.replay_ab:
        ab_results = await run_prompt_version_ab(week, candidate_versions=args.ab_versions)
        await persist_prompt_version_results(ab_results, week_iso)

    # 4. Compose markdown report
    report_md = render_markdown_report(
        week_iso=week_iso, week=week,
        pnl_attribution=pnl_attribution,
        calibration_drift=calibration_drift,
        cost_correct=cost_correct,
        regression_results=regression_results,
        ab_results=ab_results,
    )

    out_path = Path(f"reports/weekly/{week_iso}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_md)

    # 5. Telegram digest
    await send_telegram_digest(week_iso, week, pnl_attribution, calibration_drift)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", type=str, help="ISO week e.g. 2026-W17")
    parser.add_argument("--replay-ab", action="store_true",
        help="Also run prompt-version A/B replays for staged candidate versions")
    parser.add_argument("--ab-versions", action="append", default=[],
        help="Candidate persona versions to A/B, e.g. fno_expert=v2")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args)))
```

### 3.2 P&L attribution (the most important block)

The weekly notebook's central question: **of the week's P&L, how much was
caused by prompt changes vs market regime changes vs randomness?**

The decomposition:

```python
async def compute_pnl_attribution(week: WeekData) -> dict:
    """Decompose the week's P&L into attributable buckets.

    Method:
      1. Compute realised_pnl_pct per workflow_run from agent_predictions_outcomes
      2. For each run, identify the prompt_versions used (from
         agent_predictions.prompt_versions JSONB)
      3. Group by week:
         - 'baseline_runs': runs using the prior week's prompt versions
         - 'experimental_runs': runs using a newly-deployed prompt version
      4. Within each group, compute mean P&L vs control (last 30 days mean).
      5. Attribute: (experimental_mean - baseline_mean) / week's_total_pnl
         = % of week's P&L attributable to prompt changes vs other.
    """
    runs = week.workflow_runs
    deltas_by_version_change = defaultdict(list)

    for r in runs:
        prompts_today = r.prompt_versions
        prompts_baseline = await fetch_baseline_prompt_versions(
            workflow_name=r.workflow_name, until=week.start
        )
        changes = {k: v for k, v in prompts_today.items()
                   if prompts_baseline.get(k) != v}
        for changed_agent, new_version in changes.items():
            deltas_by_version_change[(changed_agent, new_version)].append(r.realised_pnl_pct)

    attribution = []
    for (agent, version), pnls in deltas_by_version_change.items():
        baseline_pnls = await fetch_baseline_pnls(agent, version, lookback_days=30)
        delta = mean(pnls) - mean(baseline_pnls)
        attribution.append({
            "agent": agent, "version": version,
            "n_runs_using_version": len(pnls),
            "mean_pnl_with_version": mean(pnls),
            "mean_pnl_baseline": mean(baseline_pnls),
            "delta_pp": delta,
            "is_significant": _is_significant(pnls, baseline_pnls),  # t-test
        })

    return {
        "week_total_pnl_pct": sum(r.realised_pnl_pct for r in runs),
        "attribution": attribution,
        "unattributed_pp": _compute_residual(attribution, runs),
        "regime_context": await _fetch_regime_context(week),  # VIX, Nifty, sector breadth
    }
```

The `regime_context` — VIX trajectory, Nifty trend, FII flows for the week —
is what lets the operator distinguish "prompt v2 is better" from "prompt v2
just happened to run during a bull week". The full markdown report includes
both deltas with their regime context, so the operator can decide.

### 3.3 Calibration drift

```python
def compute_calibration_drift(week: WeekData) -> dict:
    """Did the week's predictions show systematic over- or under-confidence?

    Method:
      For each resolved prediction:
        - The agent stated conviction X (0-1)
        - The realised outcome was win (1) or loss (0)
      Bin predictions by conviction (0.5-0.6, 0.6-0.7, etc.) and compare
      bin's win rate to bin's expected win rate.
      A well-calibrated agent has bin_win_rate ≈ bin_midpoint for every bin.
    """
    bins = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]
    drift_by_agent = defaultdict(dict)

    for prediction in week.resolved_predictions:
        agent = "ceo_judge"  # the calibration that matters most is the final
        conv = prediction.conviction
        won = prediction.realised_pnl_pct > 0
        for lo, hi in bins:
            if lo <= conv < hi:
                drift_by_agent[agent].setdefault((lo, hi), []).append(won)

    drift_summary = {}
    for agent, bins_data in drift_by_agent.items():
        drift_summary[agent] = []
        for (lo, hi), outcomes in bins_data.items():
            n = len(outcomes)
            if n < 3:
                continue  # too few samples
            actual_rate = sum(outcomes) / n
            expected_rate = (lo + hi) / 2
            drift_summary[agent].append({
                "bin": f"{lo:.1f}-{hi:.1f}",
                "n": n,
                "actual_win_rate": actual_rate,
                "expected_win_rate": expected_rate,
                "delta": actual_rate - expected_rate,
                "verdict": (
                    "well_calibrated" if abs(actual_rate - expected_rate) < 0.10
                    else "overconfident" if expected_rate > actual_rate
                    else "underconfident"
                ),
            })
    return drift_summary
```

### 3.4 Cost per correct prediction

```python
def compute_cost_per_correct_prediction(week: WeekData) -> dict:
    """How much did each correct (win) prediction cost us in LLM spend?

    Decompose by:
      - Total: total_llm_cost / count(predictions where realised_pnl_pct > 0)
      - Per-workflow: same, broken out by workflow_name
      - Per-agent: which agents drive the most cost per win

    Trend: compare to last 4 weeks' rolling average. Rising trend = either
    quality dropped or workflow expanded scope.
    """
    total_cost = sum(r.cost_usd for r in week.workflow_runs)
    n_wins = sum(1 for p in week.resolved_predictions if p.realised_pnl_pct > 0)
    n_total = len(week.resolved_predictions)

    cost_per_win = total_cost / n_wins if n_wins else None
    cost_per_prediction = total_cost / n_total if n_total else None
    win_rate = n_wins / n_total if n_total else 0

    by_agent = defaultdict(Decimal)
    for ar in week.agent_runs:
        by_agent[ar.agent_name] += ar.cost_usd

    return {
        "total_llm_cost_usd": total_cost,
        "n_predictions": n_total,
        "n_wins": n_wins,
        "win_rate_pct": win_rate * 100,
        "cost_per_prediction_usd": cost_per_prediction,
        "cost_per_win_usd": cost_per_win,
        "by_agent": dict(by_agent),
        "trend_4w": await _compute_4w_trend(week),
    }
```

### 3.5 The regression suite

The regression suite is a *fixed* set of historical workflow_runs that we
re-evaluate every week to detect prompt drift on known cases.

```python
# tests/eval/seeds/regression_suite.json
{
  "version": "1.0",
  "seeds": [
    {
      "id": "seed_001_banknifty_rate_cut",
      "workflow_run_id": "uuid-from-2025-12-15",
      "rationale": "Bank Nifty rate-cut day — strong directional setup that worked",
      "expected_outcomes": {
        "ceo_judge_decision_summary_contains": ["BANKNIFTY", "long", "rate"],
        "expected_book_pnl_pct_min": 8,
        "expected_book_pnl_pct_max": 18,
        "must_use_strategy": "long_call",
        "must_not_be_REFUSED": true
      },
      "tags": ["high_conviction", "directional", "macro_catalyst"]
    },
    {
      "id": "seed_002_high_vix_cautious",
      "workflow_run_id": "uuid-from-2026-02-04",
      "rationale": "VIX 23 day with no clear catalyst — should produce skip_today=true",
      "expected_outcomes": {
        "brain_triage_skip_today": true,
        "skip_reason_contains": ["VIX"]
      },
      "tags": ["regime_skip", "calibration"]
    },
    {
      "id": "seed_003_repeat_mistake_detection",
      "workflow_run_id": "uuid-from-2026-03-12",
      "rationale": "TATAMOTORS thesis that lost 3 days in a row — should trigger do_not_repeat",
      "expected_outcomes": {
        "explorer_aggregator_do_not_repeat_count_min": 1,
        "ceo_judge_explicit_skips_contains": ["TATAMOTORS"]
      },
      "tags": ["novelty", "do_not_repeat"]
    }
    // ... 12-15 seeds covering: high-conviction wins, conservative skips,
    // novelty/repeat-mistake detection, guardrail edge cases, regime transitions
  ]
}
```

### 3.6 Running the regression suite

```python
async def run_regression_suite() -> list[dict]:
    """Re-run every seed via faithful replay (no overrides), then check
    expected_outcomes against the new run's outputs.

    Why faithful replay (no API spend) checks for drift?
      Because prompt versions in PERSONA_MANIFEST may have moved forward since
      the seed was captured. Faithful replay uses TODAY's PERSONA_MANIFEST
      (i.e. today's prompts) on YESTERDAY's inputs (from llm_audit_log) —
      revealing whether prompt iterations have broken behavior on known cases.

    NOTE: faithful replay still calls the API for any agent whose persona
    version was bumped since the seed. The cost is bounded by the number of
    prompt-version changes in the past week (typically 0-2 agents).
    """
    seeds = load_seeds("tests/eval/seeds/regression_suite.json")
    results = []
    for seed in seeds:
        try:
            replay_run = await replay_workflow_run(
                runner=get_runner(),
                original_workflow_run_id=seed["workflow_run_id"],
                persona_version_override=None,  # use current PERSONA_MANIFEST defaults
            )
            outcome = check_expected_outcomes(replay_run, seed["expected_outcomes"])
            results.append({
                "seed_id": seed["id"],
                "rationale": seed["rationale"],
                "tags": seed["tags"],
                "passed": outcome["all_passed"],
                "failures": outcome["failures"],
                "replay_run_id": replay_run.workflow_run_id,
            })
        except Exception as e:
            results.append({
                "seed_id": seed["id"],
                "passed": False,
                "failures": [f"Replay crashed: {e}"],
            })
    return results
```

A regression failure is a HARD signal — a prompt iteration broke a known case.
The weekly report flags these prominently and Telegram-alerts on first detection.

### 3.7 Prompt-version A/B (optional, on `--replay-ab`)

```python
async def run_prompt_version_ab(
    week: WeekData, candidate_versions: list[str]
) -> list[dict]:
    """Replay a sample of the week's runs with each candidate version and
    compare outcomes. Caller specifies versions like ['fno_expert=v2',
    'news_finder=v3']; this function runs them on the same set of original
    workflow_run_ids as faithful baselines.

    Sample size: 5 runs per candidate version (configurable). Cost: 5 ×
    (cost of just the overridden agent) per candidate, since faithful replay
    serves the rest from cache.

    Caveat: A/B with N=5 is INDICATIVE, not statistically definitive. Use
    only for filtering candidates worth a longer evaluation; a real prompt
    promotion requires a 4-week shadow run or 30+ replays.
    """
    sample_runs = _sample_diverse_runs(week.workflow_runs, n=5)
    ab_results = []

    for version_spec in candidate_versions:
        agent, version = version_spec.split("=", 1)
        version_runs = []
        for original_run in sample_runs:
            replay = await replay_workflow_run(
                runner=get_runner(),
                original_workflow_run_id=original_run.id,
                persona_version_override={agent: version},
            )
            version_runs.append({
                "original_run_id": original_run.id,
                "original_decision": original_run.judge_decision_summary,
                "replay_run_id": replay.workflow_run_id,
                "replay_decision": replay.judge_decision_summary,
                "decision_changed": (
                    original_run.judge_decision_summary != replay.judge_decision_summary
                ),
                "expected_pnl_delta_pp": (
                    replay.expected_book_pnl_pct - original_run.expected_book_pnl_pct
                ),
            })
        ab_results.append({
            "agent": agent, "candidate_version": version,
            "n_replays": len(version_runs),
            "n_decisions_changed": sum(1 for r in version_runs if r["decision_changed"]),
            "mean_expected_pnl_delta_pp": mean(
                r["expected_pnl_delta_pp"] for r in version_runs
            ),
            "replays": version_runs,
        })
    return ab_results
```

### 3.8 The `prompt_version_results` table

```sql
CREATE TABLE IF NOT EXISTS prompt_version_results (
    id                       UUID PRIMARY KEY,
    agent_name               TEXT NOT NULL,
    candidate_version        TEXT NOT NULL,
    week_iso                 TEXT NOT NULL,                  -- e.g. '2026-W18'
    n_replays                INTEGER NOT NULL,
    n_decisions_changed      INTEGER NOT NULL,
    mean_expected_pnl_delta_pp NUMERIC(6,3),
    raw_results              JSONB NOT NULL,                 -- full per-replay detail
    promotion_recommended    BOOLEAN DEFAULT false,
    promotion_reason         TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_name, candidate_version, week_iso)
);

CREATE INDEX prompt_version_results_promotion_idx
    ON prompt_version_results(promotion_recommended) WHERE promotion_recommended = true;
```

The `promotion_recommended` flag is set when:
- `n_replays >= 5`
- `mean_expected_pnl_delta_pp > 1.0` (meaningful improvement)
- A/B run on at least 2 different `regime_context` weeks (cross-regime validity)

When recommended, the operator sees it in the weekly report and decides whether
to promote the version in `PERSONA_MANIFEST` for the next week.

---

## §4 The weekly markdown report format

```markdown
# Laabh Weekly Postmortem — 2026-W18

**Period:** 2026-04-30 to 2026-05-06 (5 trading days)
**Workflows run:** 5 successful, 0 failed, 0 skipped
**Total LLM spend:** $14.82
**Total predictions:** 17 (11 F&O, 6 equity)
**Resolved predictions:** 14 (3 still open)

---

## Headline Numbers

| Metric | This week | 4-week avg | Trend |
|---|---|---|---|
| Win rate | 64% (9/14) | 58% | ↑ |
| Mean realised P&L per prediction | +5.4% | +3.1% | ↑ |
| Cost per win | $1.65 | $2.10 | ↓ (good) |
| Cost per prediction | $1.06 | $1.22 | ↓ |
| 3-day rolling shadow eval avg | 7.4/10 | 7.1/10 | ↑ |

## P&L Attribution

This week's total book P&L: **+11.8%** (target: +10%, stretch: +18%)

Of the +1.8pp above target:
- **+0.7pp** attributable to the `news_editor=v1.1` deployed Mon — stricter
  D-grade gate filtered out two TATAMOTORS chatter runs that would have lost.
- **-0.3pp** attributable to `fno_expert=v1` (no change) — calibration drift
  observed (over-confident on debit spreads in low-VIX regime).
- **+1.4pp** unattributed — likely regime tailwind (Nifty +2.4% for the week,
  VIX dropped from 17.8 to 13.1).

Bottom line: prompt changes contributed ~0.4pp net positive; the rest was
favourable regime. **Do not treat this week's win as evidence the prompts
are well-calibrated.** See calibration drift below.

## Calibration Drift (CEO Judge)

| Conviction bin | n | Actual win rate | Expected | Delta | Verdict |
|---|---|---|---|---|---|
| 0.6–0.7 | 4 | 50% | 65% | -15pp | overconfident |
| 0.7–0.8 | 6 | 67% | 75% | -8pp | overconfident |
| 0.8–0.9 | 3 | 100% | 85% | +15pp | underconfident |
| 0.9–1.0 | 1 | 100% | 95% | +5pp | (n too low) |

Pattern: the Judge is overconfident in the medium band (0.6-0.8) and slightly
underconfident at high conviction. Suggests the prompt's conviction ladder
needs recalibration in the middle. Action: stage a `ceo_judge=v2` with
sharper conviction-evidence binding and A/B for 2 weeks.

## Regression Suite Results

| Seed | Tags | Replay status | Notes |
|---|---|---|---|
| seed_001_banknifty_rate_cut | high_conviction, directional | ✅ passed | Decision unchanged |
| seed_002_high_vix_cautious | regime_skip | ✅ passed | skip_today=true preserved |
| seed_003_repeat_mistake_detection | novelty | ⚠️ DEGRADED | do_not_repeat now contains 0 items (was 2). Likely caused by `news_editor=v1.1` over-filtering — check whether do_not_repeat sources stayed accessible. |
| ... | | | |

3 of 15 seeds degraded — investigate `news_editor=v1.1` interaction with
`explorer_aggregator`'s do_not_repeat construction.

## Top Findings (for the briefing)

1. **Conviction calibration miss in the 0.6-0.8 band** — prompt iteration warranted
2. **Editor v1.1 unintended consequence** on Explorer aggregation — debug
3. **Cost per win dropped 21%** — cache hits are working
4. **Shadow eval flagged 1 run** (2026-05-04) for self-consistency: brain
   triage didn't include INFY but it appeared in Judge allocation. Bug logged.
5. **A/B run pending**: `fno_expert=v2` shows +0.8pp expected_pnl in 5 replays
   — needs 2-week shadow before promotion

## A/B Replay Results (--replay-ab was set)

### `fno_expert=v2` vs v1 (n=5 replays this week)

| Metric | v1 (baseline) | v2 (candidate) | Delta |
|---|---|---|---|
| Mean expected_book_pnl_pct | 8.4% | 9.2% | +0.8pp |
| n decisions changed | — | 3 of 5 | — |
| Mean conviction | 0.71 | 0.74 | +0.03 |
| REFUSED rate | 20% | 0% | -20pp |

⚠️ v2 refuses zero trades on the sample — might be over-eager. Recommend
2-week shadow run before promotion.

## Cost Trend

(spark line of daily LLM cost over 4 weeks — shows declining trend with
caching deployment in W17)

| Week | Total | Per win |
|---|---|---|
| W15 | $19.40 | $2.40 |
| W16 | $18.70 | $2.05 |
| W17 | $16.30 | $1.85 |
| W18 | **$14.82** | **$1.65** |

## Open Items for Next Week

1. Investigate seed_003 degradation
2. Decide whether to promote `fno_expert=v2`
3. Stage `ceo_judge=v2` with calibration recalibration
4. Review the 1 self-consistency bug (2026-05-04)
```

The report is rendered by `src/eval/weekly_renderer.py` from a Jinja2 template.

---

## §5 Telegram digest format

The Sunday digest is intentionally *short* — for at-a-glance Sunday evening
reading on mobile. Full report goes to `reports/weekly/`.

```
📊 Laabh W18 Postmortem

P&L: +11.8% (vs +10% target) ✅
  • +0.4pp prompt changes
  • +1.4pp regime tailwind

Win rate: 64% (9/14) ↑
Cost per win: $1.65 ↓21%

⚠️ Findings:
  1. Judge overconfident in 0.6-0.8 band
  2. seed_003 regression DEGRADED
  3. fno_expert=v2 shows +0.8pp; needs 2-wk shadow

Full report: reports/weekly/2026-W18.md
```

---

## §6 Operational schedule

```
Monday-Friday 09:00 IST  →  predict_today_combined runs
                            (shadow_evaluator runs as final stage of each)

Monday-Friday 16:00 IST  →  evaluate_yesterday workflow runs
                            (resolves prior-day predictions, populates
                             agent_predictions_outcomes)

Sunday 18:00 IST        →   weekly_postmortem.py runs (no --replay-ab by default)

First Sunday of month    →  weekly_postmortem.py --replay-ab
                            with all currently-staged candidate versions

Quarterly               →   manual deep dive: rebuild regression suite from
                            latest 90 days of resolved predictions, retire
                            stale seeds, add new edge cases
```

cron entries:

```cron
# Mon-Fri 09:00 IST = 03:30 UTC
30 3 * * 1-5  cd /home/laabh && /usr/bin/python -m src.scheduler run predict_today_combined

# Mon-Fri 16:00 IST = 10:30 UTC
30 10 * * 1-5 cd /home/laabh && /usr/bin/python -m src.scheduler run evaluate_yesterday

# Sun 18:00 IST = 12:30 UTC
30 12 * * 0   cd /home/laabh && /usr/bin/python scripts/weekly_postmortem.py
```

---

## §7 DB migration summary

```sql
-- Migration: 0XX_eval_tables.sql

-- 1. Shadow eval per workflow_run
CREATE TABLE agent_predictions_eval (...);  -- see §2.3

-- 2. Outcomes (originally specified in plan §5; included for completeness)
CREATE TABLE IF NOT EXISTS agent_predictions_outcomes (
    id                       UUID PRIMARY KEY,
    prediction_id            UUID NOT NULL REFERENCES agent_predictions(id),
    resolved_at              TIMESTAMPTZ NOT NULL,
    realised_pnl_pct         NUMERIC(8,3) NOT NULL,
    hit_target               BOOLEAN,
    hit_stop                 BOOLEAN,
    exit_reason              TEXT,
    exit_price               NUMERIC(12,4),
    underlying_close_at_resolve NUMERIC(12,4),
    book_at_risk_pct         NUMERIC(6,3),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (prediction_id)
);
CREATE INDEX agent_predictions_outcomes_resolved_idx
    ON agent_predictions_outcomes(resolved_at);

-- 3. Prompt version A/B aggregation
CREATE TABLE prompt_version_results (...);  -- see §3.8

-- 4. workflow_runs extensions for replay
ALTER TABLE workflow_runs ADD COLUMN parent_run_id UUID REFERENCES workflow_runs(id);
ALTER TABLE workflow_runs ADD COLUMN experiment_tag TEXT;
ALTER TABLE workflow_runs ADD COLUMN persona_version_overrides JSONB DEFAULT '{}'::jsonb;
CREATE INDEX workflow_runs_parent_run_id_idx ON workflow_runs(parent_run_id);
CREATE INDEX workflow_runs_experiment_tag_idx
    ON workflow_runs(experiment_tag) WHERE experiment_tag IS NOT NULL;
```

---

## §8 Implementation checklist

```
1. db/migrations/0XX_eval_tables.sql                  (~120 lines)
2. src/eval/__init__.py                               (~10 lines)
3. src/eval/shadow.py — shadow eval persistence       (~150 lines)
4. src/eval/weekly.py — weekly aggregator             (~600 lines)
5. src/eval/regression.py — regression suite runner   (~150 lines)
6. src/eval/ab.py — prompt-version A/B               (~200 lines)
7. src/eval/weekly_renderer.py + Jinja2 template      (~250 lines)
8. scripts/replay_workflow_run.py                     (~80 lines)
9. scripts/weekly_postmortem.py                       (~70 lines)
10. tests/eval/seeds/regression_suite.json (12-15 seeds, ~400 lines)
11. tests/eval/test_weekly.py                         (~200 lines)
12. tests/eval/test_shadow.py                         (~150 lines)
13. tests/eval/test_replay.py                         (~120 lines)

Total: ~2,500 lines of production + tests. One PR for each major module
(shadow, weekly, regression, A/B), with the migration in its own PR first.
```

---

## §9 What this eval system does NOT do

- It does NOT make trade decisions. It only evaluates the workflow's
  internal logic and tracks outcomes.
- It does NOT auto-promote prompt versions. The operator decides based on
  the weekly report; promotion is a manual edit to PERSONA_MANIFEST.
- It does NOT recreate the convergence/auto-trader feedback loop. That
  loop (signals → outcomes → analyst credibility) lives in the existing
  Phase 1/Phase 3 pipeline; the eval system is parallel and complementary.
- It does NOT generate new prompts. Prompt iteration is human-driven; the
  eval system tells you WHEN to iterate and what to fix, not WHAT to write.

---

## §10 Closing — the four change-sets together

| Change-set | Purpose | Lines |
|---|---|---|
| #1 plan patch | Close the design gaps in the parent plan | ~390 |
| #2 prompts and tools | All 13 production agent prompts + 1 evaluator + ~17 tools + Pydantic validators | ~2,680 |
| #3 runtime | WorkflowRunner, structured-output enforcement, fallback policy, replay, validators integration | ~1,030 |
| #4 eval (this) | Live shadow + weekly postmortem + replay harness + regression suite + A/B framework | ~770 |

Together: a complete agentic workflow architecture for Laabh — from the
Brain's morning triage down to the Sunday postmortem that decides whether
this week's prompt iterations earned their P&L. Every layer is implementable
without further design work.

The phased delivery in the parent plan stays as-is. The four change-sets
above are dependency-ordered so each can be built and tested in isolation:

```
PR sequence:
  1. Plan patch (this is text + schema, no runtime code)
  2. Migration: agent_runs + workflow_runs + agent_predictions
  3. Personas and tools (all prompts and tool registrations)
  4. Runtime (WorkflowRunner + replay)
  5. First workflow: predict_today_fno (smaller surface area)
  6. Migration: agent_predictions_eval + outcomes + prompt_version_results
  7. Eval: shadow only (live shadow eval running)
  8. Eval: weekly postmortem (regression suite + reporting)
  9. Eval: A/B framework
  10. Integration: laabh-runday CLI hooks
```

Each PR <600 lines of net code, testable in isolation, releasable to a
single-user paper-trading system without coordination overhead.

---

*End of eval spec. Mega-drop complete.*
