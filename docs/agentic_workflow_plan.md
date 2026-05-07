# Laabh — Agentic Workflow Architecture Plan

**Audience:** ashusaxe007@gmail.com
**Date:** 2026-05-07
**Status:** Proposal for review
**Scope:** Re-shape Laabh's prediction layer into a reusable, agent-driven workflow system on top of the existing Phase-1 data backbone.

---

## 1. North Star

Today Laabh has the data plumbing (collectors, signals table, F&O chain, LLM audit, dryrun support). What it lacks is a **first-class predictive layer** that:

1. Treats every market prediction as a **named, versioned, replayable workflow**.
2. Composes **specialised agents** (each with a clear persona + structured I/O contract) instead of monolithic prompts.
3. Persists every agent message, tool call, and verdict so we can **debate, score, and learn from** every prediction.
4. Closes the loop: predictions → trades → P&L → next-day priors.

The deliverable today is the **architecture + plan**, not code. Implementation is staged in §8.

---

## 2. Agentic primitives

Three new primitives, one minor.

| Primitive | What it is | Why |
|---|---|---|
| **Workflow** (definition) | Versioned recipe: which agents, in what order, with what tools. Stored as code + DB row. | Reuse — "predict_today_fno_v3" is just a workflow name with knobs. |
| **WorkflowRun** | One execution of a workflow, with `as_of`, `dryrun_run_id`, inputs, final verdict. | Replayability + dryrun symmetry with current convention. |
| **AgentRun** | One agent invocation inside a WorkflowRun: persona id, prompt version, model, tool calls, structured output, tokens, latency, cost. | Debug, score per-agent, and feed `llm_audit_log`. |
| **Prediction** | Brain's final output: instrument, horizon, target, stop, conviction, reasoning, expected_pnl. | Separate from `signals` (which are extracted from external sources). |
| **PredictionOutcome** | Realised P&L vs predicted, evaluated at horizon. | Closes the learning loop; feeds the Historical Explorer. |

Existing `signals`, `fno_signals`, `strategy_decisions`, `llm_audit_log` stay — they remain the source-of-truth for **inputs** the agents read. Agents emit **predictions**.

---

## 3. Agent catalogue

Each agent is specified as: **persona** + **inputs** + **tools** + **structured output**. Persona prompts live in `src/agents/prompts/` as versioned constants (same pattern as `src/extraction/prompts.py`). Models default to Sonnet 4.6 for analysis, Haiku 4.5 for cheap fan-out, Opus 4.7 for the CEO.

### 3.1 News Finder Agent

> *"Senior data analyst and financial news expert. Pulls every relevant signal from live + historical news for one instrument or F&O underlying."*

- **Inputs:** `instrument_id` *(or `underlying_id`)*, `lookback_days` (default 7 live + 90 historical), `as_of`.
- **Tools (all DB-backed, not web):**
  - `search_raw_content(instrument_id, since, until, limit)` — joins `raw_content` ↔ `signals` ↔ `instruments`.
  - `search_transcript_chunks(symbol, since)` — uses `idx_chunks_stocks` (GIN on stock_symbols).
  - `get_filings(instrument_id, since)` — corporate filings from `raw_content` filtered by source type.
  - `get_analyst_track_record(analyst_id)` — credibility + hit rate from `analysts`.
- **Structured output:**
  ```json
  {
    "instrument": {"id": 123, "symbol": "RELIANCE"},
    "as_of": "2026-05-07T08:00:00Z",
    "narrative": "<3-paragraph rich-text analysis citing each item>",
    "themes": ["refinery margins recovering", "Jio spinoff overhang"],
    "catalysts_next_5d": [{"event": "Q4 results", "date": "2026-05-09"}],
    "risk_flags": ["promoter pledge increase"],
    "citations": [{"raw_content_id": 4456, "weight": 0.8}],
    "summary_json": {
      "sentiment": "bullish|neutral|bearish",
      "score": -1.0,
      "signal_count": {"buy": 4, "sell": 1, "hold": 2},
      "top_analyst_views": [{"analyst": "Sandip Sabharwal", "stance": "BUY", "credibility": 0.78}],
      "freshness_minutes": 42
    }
  }
  ```
- **Reuses:** `src/extraction/prompts.py` system prompt patterns; `analysts` credibility scoring; `signals.convergence_score`.

### 3.2 News Editor Agent

> *"Senior editor of a financial news network. Reviews the News Finder's output, calls out weak sourcing, ranks themes by importance, and produces an editor's note."*

- **Inputs:** News Finder's full output.
- **No tools** — pure reasoning over the input. Cheap (Haiku) or Sonnet for higher-stakes underlyings.
- **Structured output:**
  ```json
  {
    "headline": "<8-word editorial headline>",
    "lede": "<2-sentence stand-first>",
    "credibility_grade": "A|B|C|D",
    "weak_claims": ["<claim>: why it's weak"],
    "strongest_signal": {"signal_id": 9912, "why": "..."},
    "spike_or_noise": "spike|noise|mixed",
    "go_no_go_for_brain": true
  }
  ```
- **Why a separate agent:** structurally forces a second-pass critique before the brain consumes news. This is the cheapest reliability gain we'll get.

### 3.3 Historical Explorer Agent (with sub-agents)

> *"Explores existing data, past predictions, past P&L, market trends over 1w/15d/1m."*

This is the most tool-heavy agent. It dispatches **parallel sub-agents** rather than long-running serial tool loops.

- **Sub-agents (run in parallel):**
  1. **Trend Sub-agent** — pulls `price_daily` + `price_ticks` aggregates → returns trend-structure JSON for 1w/15d/1m/3m.
  2. **Past-Prediction Sub-agent** — joins `predictions` ↔ `prediction_outcomes` ↔ `instruments` for this symbol *and* its sector → win/loss, avg conviction-vs-realised gap, biggest mistakes, biggest wins.
  3. **Sentiment-Drift Sub-agent** — `market_sentiment` + `signals.convergence_score` time-series.
  4. **F&O-Positioning Sub-agent** — `options_chain` + `iv_history` + `fno_signals` resolution stats.
- **Aggregator output:**
  ```json
  {
    "instrument": {...},
    "horizon_views": {"1w": {...}, "15d": {...}, "1m": {...}},
    "past_decisions": {
      "n_predictions": 14, "win_rate": 0.57, "avg_realised_pnl_pct": 1.8,
      "biggest_mistake": {"prediction_id": 88, "lesson": "..."},
      "biggest_win": {"prediction_id": 64, "rationale_that_worked": "..."}
    },
    "signals_to_watch": ["IV crush after results", "FII flows turning"],
    "tradable_pattern_score": 0.72,
    "do_not_repeat": ["chasing breakout against sector trend"]
  }
  ```
- **Reuses:** `src/analytics/source_scorer.py`, `analyst_tracker.py`, `convergence.py`.
- **Sub-agent pattern:** implemented as `asyncio.gather()` over `claude.messages.create` calls — *not* the SDK's nested-Task tool. Cheaper, deterministic, and each sub-agent's full transcript lands in `agent_runs`.

### 3.4 Brain (orchestrator)

> *"The conductor. Calls News Finder → News Editor → Historical Explorer, then routes to the F&O Expert and Equity Expert in parallel, then to the CEO."*

- **Not a single LLM call.** The Brain is a Python orchestrator class (`src/agents/brain.py`) that owns a `WorkflowRun` row. It only invokes an LLM when it needs to **decide which symbols are worth analysing today** (a cheap Haiku triage call over the watchlist + today's top movers).
- Why deterministic glue, not an LLM-driven controller: cheaper, observable, restartable mid-flight, and the agentic creativity is in the leaves — not the wiring.

### 3.5 F&O Expert sub-agent

> *"Indian F&O expert specialising in NFO; follows known best practices and known strategies. Predicts the best F&O trades for today's session aiming for ~10% profit."*

- **Inputs (per candidate underlying):** Editor verdict, Explorer output, current `options_chain` snapshot, `iv_history`, `fno_ban_list`, `fno_calendar.next_expiry`.
- **Tools:** `get_strategy_payoff(strategy, strikes, expiry, spot)` — calls existing `src/fno/strategies/*` modules to score real payoffs (already built — `bull_call_spread`, `iron_condor`, `straddle`, etc.). Agent picks the strategy; Python computes the math.
- **Persona reuses:** `FNO_ANALYST_INDIA` from `src/integrations/vibetrade/prompts.py` — already written, India-specific.
- **Structured output (one row per candidate):**
  ```json
  {
    "underlying": "BANKNIFTY",
    "strategy": "bull_call_spread",
    "legs": [{"side": "BUY", "type": "CE", "strike": 48000, "expiry": "2026-05-08"},
             {"side": "SELL", "type": "CE", "strike": 48500, "expiry": "2026-05-08"}],
    "max_profit_pct": 12.5, "max_loss_pct": 4.2, "breakeven": 48180,
    "iv_environment": "fair", "pcr": 1.08, "max_pain": 48200,
    "conviction": 0.74,
    "expected_10pct_probability": 0.41,
    "rationale": "<3 sentences>",
    "stop_rule": "exit if BankNifty < 47800 by 12:30 IST"
  }
  ```
- **Hard guardrails (enforced in Python, not the prompt):** reject if underlying in `fno_ban_list`, reject if max-loss > portfolio risk budget, reject if expiry > 2 weeks for an "intraday-aiming-10%" candidate.

### 3.6 Equity Expert sub-agent

> *"Equity trading expert. Analyses equities for ~10% profit and assigns a score."*

- **Inputs:** Editor + Explorer output, `price_daily` 200-day window, `holdings`, sector exposure, `market_sentiment`.
- **Tools:** `score_technicals(symbol)` (RSI/MACD/BB from `price_daily`), `score_fundamentals(symbol)` (latest filing snapshot), `position_sizing(account_value, target_pct, stop_pct)`.
- **Structured output:**
  ```json
  {
    "symbol": "TATAMOTORS",
    "thesis": "<2-sentence>",
    "entry_zone": [842, 848], "target": 935, "stop": 815,
    "expected_return_pct": 10.4, "horizon_days": 5,
    "score": 0.81,
    "score_components": {"technical": 0.78, "fundamental": 0.65, "sentiment": 0.88, "regime_fit": 0.84},
    "size_pct_of_portfolio": 4.0
  }
  ```

### 3.7 CEO / Strategist (final agent)

> *"CEO and strategist of a large investment firm dealing with hedge funds focused on daily gains. Debates and analyses the F&O and Equity predictions, picks the best path to 10% today."*

- **Model:** Opus 4.7 (this is the only call we want to spend top-shelf tokens on).
- **Inputs:** ranked F&O candidates, ranked equity candidates, current portfolio snapshot, India VIX, NIFTY 1-day & week regime from `regime_gate.py`.
- **Pattern:** **structured debate, not free chat.** Two passes:
  1. **Pass A — Bull/Bear sparring:** Opus is asked to argue *for* the top F&O pick and *against* it; same for top equity pick. (One call, 4-section output.)
  2. **Pass B — CEO verdict:** Opus consumes Pass A and emits the final allocation.
- **Final structured output (becomes a row in `predictions`):**
  ```json
  {
    "workflow_run_id": "...",
    "decision_date": "2026-05-07",
    "allocation": [
      {"asset_class": "fno", "underlying": "BANKNIFTY", "strategy": "bull_call_spread", "capital_pct": 35, ...},
      {"asset_class": "equity", "symbol": "TATAMOTORS", "capital_pct": 25, ...},
      {"asset_class": "cash", "capital_pct": 40, "reason": "VIX 18, mid-week, FOMC tonight"}
    ],
    "expected_book_pnl_pct": 6.2,
    "stretch_pnl_pct": 11.0,
    "max_drawdown_tolerated_pct": 3.0,
    "kill_switch": "exit all if NIFTY breaks below 22480",
    "ceo_note": "<5-sentence narrative>"
  }
  ```
- **Reuses:** `src/integrations/tradingagents/debate.py` is conceptually similar — keep it as a *fallback debate engine*, but the CEO pass is our own (because we need the final allocation row, not just a Buy/Hold verdict).

---

## 4. Workflows (the reusable layer)

Workflows are Python classes in `src/agents/workflows/` AND a row in `workflows` (id, name, version, schema, default_params). Calling `WorkflowRunner.run("predict_today_fno", as_of=..., params={...})` always:

1. Inserts a `workflow_runs` row (status=running).
2. Streams every agent message into `agent_runs`.
3. Writes the final verdict to `predictions`.
4. Marks `workflow_runs.status = succeeded|failed`.

### Initial workflow set

| Name | Purpose | Cron |
|---|---|---|
| `predict_today_fno` | F&O daily play. Brain → News→Editor→Explorer→F&O Expert (per-underlying parallel) → CEO. | 09:00 IST |
| `predict_today_equity` | Equity intraday/swing picks. Same shape, equity expert. | 09:00 IST |
| `predict_today_combined` | Composes the two and runs CEO once over the union. | 09:05 IST (after the two above) |
| `analyse_one_instrument` | Ad-hoc deep-dive (the API endpoint behind a UI "analyse this stock" button). | on-demand |
| `evaluate_yesterday` | Pulls yesterday's predictions, joins with realised prices, writes `prediction_outcomes`. | 16:00 IST |
| `weekly_postmortem` | Aggregates last 7 days of outcomes → narrative report → email/Telegram. | Sun 18:00 IST |

All workflows accept the standard `as_of: datetime | None` and `dryrun_run_id: uuid.UUID | None` params (per the existing CLAUDE.md convention).

---

## 5. Database schema additions

New tables in a single migration `database/migrations/2026_05_07_agentic_workflows.sql`. Field types match existing conventions (UUIDs, `TIMESTAMPTZ`, `NUMERIC`).

```sql
-- 5.1 Workflow definitions (versioned)
CREATE TABLE workflows (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL,
  version       INTEGER NOT NULL,
  description   TEXT,
  agent_chain   JSONB NOT NULL,          -- ordered list of agent specs
  default_params JSONB NOT NULL DEFAULT '{}',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (name, version)
);

-- 5.2 Each execution
CREATE TABLE workflow_runs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id     UUID NOT NULL REFERENCES workflows(id),
  workflow_name   TEXT NOT NULL,             -- denormalised for fast filtering
  as_of           TIMESTAMPTZ NOT NULL,
  dryrun_run_id   UUID,                      -- nullable: live runs use NULL
  params          JSONB NOT NULL DEFAULT '{}',
  status          TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled')),
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at     TIMESTAMPTZ,
  total_tokens    INTEGER,
  total_cost_usd  NUMERIC(10,4),
  error           TEXT
);

-- 5.3 Each agent invocation inside a run
CREATE TABLE agent_runs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_run_id   UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  parent_agent_run_id UUID REFERENCES agent_runs(id),  -- for sub-agents
  agent_name        TEXT NOT NULL,         -- 'news_finder', 'fno_expert', ...
  persona_version   TEXT NOT NULL,         -- prompt version constant
  model             TEXT NOT NULL,
  inputs            JSONB,
  output            JSONB,
  tool_calls        JSONB,                 -- array of {name, args, result}
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  cost_usd          NUMERIC(10,4),
  latency_ms        INTEGER,
  status            TEXT NOT NULL,         -- 'ok' | 'error' | 'rejected_by_guardrail'
  error             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5.4 Brain's output rows
CREATE TABLE predictions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_run_id   UUID NOT NULL REFERENCES workflow_runs(id),
  asset_class       TEXT NOT NULL CHECK (asset_class IN ('equity','fno','cash')),
  instrument_id     INTEGER REFERENCES instruments(id),  -- nullable for cash
  underlying_id     INTEGER REFERENCES instruments(id),  -- for fno
  decision_date     DATE NOT NULL,
  horizon_days      INTEGER NOT NULL,
  entry_zone_low    NUMERIC(12,2),
  entry_zone_high   NUMERIC(12,2),
  target_price      NUMERIC(12,2),
  stop_price        NUMERIC(12,2),
  expected_pnl_pct  NUMERIC(6,2),
  conviction        NUMERIC(4,3),          -- 0..1
  capital_pct       NUMERIC(6,2),
  fno_strategy      TEXT,
  fno_legs          JSONB,
  rationale         TEXT NOT NULL,
  kill_switch       TEXT,
  superseded_by     UUID REFERENCES predictions(id),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5.5 Realised vs predicted (closes the loop)
CREATE TABLE prediction_outcomes (
  prediction_id     UUID PRIMARY KEY REFERENCES predictions(id) ON DELETE CASCADE,
  evaluated_at      TIMESTAMPTZ NOT NULL,
  realised_pnl_pct  NUMERIC(8,3),
  hit_target        BOOLEAN,
  hit_stop          BOOLEAN,
  exit_reason       TEXT,                  -- 'target'|'stop'|'time_exit'|'manual'
  notes             TEXT
);
```

---

## 6. Indexes — by query layer

(*Senior DBA hat on.* I'll be specific about which agent's query each index is for, because that's how indexes age well.)

### 6.1 News Finder hot path

```sql
-- "Give me everything published about RELIANCE in the last 7 days" — currently scans raw_content+signals separately.
CREATE INDEX idx_signals_instrument_date ON signals (instrument_id, signal_date DESC) INCLUDE (action, confidence);
CREATE INDEX idx_raw_content_published_partial ON raw_content (published_at DESC) WHERE is_processed = TRUE;
-- Existing idx_raw_content_processed (boolean) is low cardinality; replace with the partial above for unprocessed.
DROP INDEX IF EXISTS idx_raw_content_processed;
CREATE INDEX idx_raw_content_unprocessed ON raw_content (created_at) WHERE is_processed = FALSE;

-- Transcript-chunk lookup by symbol — already has GIN, add a covering combo:
CREATE INDEX idx_chunks_stocks_time ON transcript_chunks USING gin (stock_symbols, to_tsvector('english', text));
```

### 6.2 Historical Explorer

```sql
-- Past-Prediction sub-agent: "all predictions for instrument X, joined to outcomes"
CREATE INDEX idx_predictions_instrument_date ON predictions (instrument_id, decision_date DESC);
CREATE INDEX idx_predictions_underlying_date ON predictions (underlying_id, decision_date DESC) WHERE underlying_id IS NOT NULL;
CREATE INDEX idx_predictions_workflow ON predictions (workflow_run_id);
-- Outcomes already have predictions PK; for "recent realised P&L by sector" join through instruments:
CREATE INDEX idx_prediction_outcomes_eval ON prediction_outcomes (evaluated_at DESC);

-- Trend sub-agent on price_daily (continuous aggregate is ideal but index for ad-hoc):
CREATE INDEX idx_price_daily_instrument_date ON price_daily (instrument_id, date DESC) INCLUDE (close, volume);

-- price_ticks (TimescaleDB hypertable) — add space-then-time composite if missing:
CREATE INDEX IF NOT EXISTS idx_ticks_instrument_time ON price_ticks (instrument_id, time DESC);
-- And a BRIN on cold partitions to cut I/O on >30d backfills:
CREATE INDEX IF NOT EXISTS idx_ticks_time_brin ON price_ticks USING BRIN (time);
```

### 6.3 F&O Expert

```sql
-- Latest chain snapshot per (underlying, expiry):
CREATE INDEX idx_options_chain_underlying_expiry_time
  ON options_chain (underlying_id, expiry_date, snapshot_time DESC);

-- IV history time-series:
CREATE INDEX idx_iv_history_underlying_date ON iv_history (underlying_id, as_of_date DESC);

-- F&O ban check is a hot path before every recommendation:
CREATE INDEX idx_fno_ban_lookup ON fno_ban_list (instrument_id, ban_date DESC);

-- F&O signal status board:
CREATE INDEX idx_fno_signals_underlying_status ON fno_signals (underlying_id, status, proposed_at DESC);
```

### 6.4 CEO / postmortem

```sql
-- Workflow audit & cost rollups:
CREATE INDEX idx_workflow_runs_name_started ON workflow_runs (workflow_name, started_at DESC);
CREATE INDEX idx_workflow_runs_dryrun ON workflow_runs (dryrun_run_id) WHERE dryrun_run_id IS NOT NULL;

-- Agent-level slice-and-dice ("show me all F&O Expert outputs that were rejected_by_guardrail this week"):
CREATE INDEX idx_agent_runs_workflow ON agent_runs (workflow_run_id, agent_name);
CREATE INDEX idx_agent_runs_status_created ON agent_runs (status, created_at DESC) WHERE status <> 'ok';
CREATE INDEX idx_agent_runs_agent_time ON agent_runs (agent_name, created_at DESC);

-- LLM audit (already exists) — add covering for cost queries:
CREATE INDEX IF NOT EXISTS idx_llm_audit_caller_cost ON llm_audit_log (caller, created_at DESC) INCLUDE (cost_usd, total_tokens);
```

### 6.5 Continuous aggregates (TimescaleDB) — better than indexes for the trend agent

```sql
CREATE MATERIALIZED VIEW price_daily_1w
WITH (timescaledb.continuous) AS
SELECT instrument_id, time_bucket('1 day', time) AS day,
       first(price, time) AS open, max(price) AS high, min(price) AS low,
       last(price, time) AS close, sum(volume) AS volume
FROM price_ticks GROUP BY instrument_id, day;
-- Refresh policy hourly during market hours; daily otherwise.
```

This kills the per-symbol "give me 1w/15d/1m OHLC" query that the Trend sub-agent runs N times a workflow.

### 6.6 Index hygiene

- Add a monthly `pg_stat_user_indexes` review job (`scripts/index_audit.py`) that flags zero-scan indexes after 30 days.
- Use `EXPLAIN (ANALYZE, BUFFERS)` baselines per agent's hot query — store baselines in `tests/perf/`.
- Avoid index creep: every new agent must declare which existing index covers its tools, or PR the new one.

---

## 7. What we already have vs what's new

| Component | Status | Action |
|---|---|---|
| `src/extraction/prompts.py` | ✅ exists | Reuse system prompt patterns; News Finder picks up batch extraction. |
| `src/integrations/vibetrade/prompts.py` (FUNDAMENTALS/SENTIMENT/TECHNICAL/FNO_ANALYST_INDIA) | ✅ exists | Reuse — these become the persona prompts for the sub-agents inside Editor & F&O Expert. |
| `src/integrations/tradingagents/debate.py` | ✅ exists | Keep as fallback debate; CEO is new because we need allocation output, not Buy/Hold. |
| `src/fno/strategies/*` (bull_call_spread, iron_condor, straddle, …) | ✅ exists | F&O Expert calls these as **tools** — the LLM picks the strategy, Python computes payoff. |
| `src/fno/ban_list.py`, `calendar.py`, `chain_parser.py`, `iv_history_builder.py` | ✅ exists | Become the F&O Expert's data tools. |
| `src/analytics/source_scorer.py`, `analyst_tracker.py`, `convergence.py` | ✅ exists | Power the News Finder's `get_analyst_track_record` and Editor's `credibility_grade`. |
| `src/laabh/regime_gate.py` | ✅ exists | CEO consumes its output as a kill-switch input. |
| `llm_audit_log` table | ✅ exists | `agent_runs` writes to it via the existing `caller`/`caller_ref_id` fields. |
| `strategy_decisions` (with `dryrun_run_id`) | ✅ exists | `predictions` is similar but agent-specific; we keep `strategy_decisions` for the auto-trader's downstream actions. |
| `WorkflowRunner` + `BrainOrchestrator` + `AgentRun` writer | ❌ new | §8 Phase 1. |
| `workflows`/`workflow_runs`/`agent_runs`/`predictions`/`prediction_outcomes` tables | ❌ new | §8 Phase 1. |
| Continuous aggregate `price_daily_1w` | ❌ new | §8 Phase 2. |
| `evaluate_yesterday` workflow | ❌ new | §8 Phase 3. |

---

## 8. Phased delivery

### Phase 1 — Foundations (1–2 days of work)
1. Migration `2026_05_07_agentic_workflows.sql` (§5).
2. `src/agents/runtime.py` — `WorkflowRunner`, `AgentRun` writer, structured-output schema validation (Pydantic per-agent).
3. `src/agents/agents/news_finder.py` + `news_editor.py` (no historical sub-agents yet).
4. Slim integration test: `analyse_one_instrument("RELIANCE")` returns persisted rows.

### Phase 2 — Predictive layer (2–3 days)
5. Historical Explorer + 4 sub-agents in parallel.
6. F&O Expert + Equity Expert with their tools.
7. Workflows `predict_today_fno` and `predict_today_equity`.
8. Continuous aggregate `price_daily_1w`.

### Phase 3 — CEO + closed loop (2 days)
9. CEO debate + verdict, writes `predictions`.
10. `evaluate_yesterday` workflow + `prediction_outcomes`.
11. APScheduler entries for the daily cadence.
12. Telegram report at end of `evaluate_yesterday`.

### Phase 4 — Reliability (1 day)
13. Per-agent guardrails (ban-list reject, max-loss reject, kill-switch enforcement).
14. Cost dashboard query + `tests/perf/` baselines for hot indexes.
15. Index-audit cron.

---

## 9. Open questions for you (please confirm before Phase 1)

1. **Capital base for "10% profit"** — 10% of *deployed capital* per workflow run, or 10% of *total portfolio value*? The CEO's allocation depends on this.
2. **Risk budget** — what's the max acceptable single-day drawdown? 3%? 5%?
3. **F&O scope** — index options only (Nifty, BankNifty, FinNifty), or also stock F&O? Stock F&O changes the data needs significantly.
4. **CEO model spend** — happy with Opus 4.7 for the daily CEO call (~$0.50–$1.50/day estimate), or cap at Sonnet 4.6?
5. **Live-trading hand-off** — does the CEO's `predictions` row auto-flow into `pending_orders`, or is there a manual approval step?

---

## 10. Non-goals (intentionally not in this plan)

- Web search / live news scraping inside agents — they read `raw_content`, the collectors fill it. Keeps cost bounded and reasoning auditable.
- Reinforcement-learning / online fine-tuning of agents. The learning loop is via prompt iteration informed by `prediction_outcomes`, not weight updates.
- Replacing the existing `signals` pipeline. Signals stay; they're agents' best raw input.
- A general-purpose "controller LLM" deciding workflow shape at runtime. The Brain is deterministic Python; only leaves are LLMs.

---

*End of plan. Feedback / counterproposals welcome before any code lands.*
