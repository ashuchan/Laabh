# `src/fno/` — Chain Ingestion Retrofit Modules

## Purpose

This package contains the NSE-primary chain ingestion orchestration layer that
replaced the former Angel One chain source.  The three modules here drive
everything above the raw source adapters (`src/fno/sources/`) and below the
scheduler and API layers.

Angel One has **no option chain endpoint** and its WebSocket cap (3,000 tokens)
is 8× below the ~24,000 tokens required for the full F&O universe.  Angel One
continues to be used for underlying ticks, India VIX, and the per-strike Greeks
API only.

---

## Module layout

```
src/fno/
├── chain_collector.py   NSE→Dhan failover orchestration + DB persistence
├── tier_manager.py      Daily tier classification (Tier 1 / Tier 2)
├── issue_filer.py       Daily review loop: GitHub issue dedup + Telegram
├── chain_parser.py      Greek enrichment (Black-Scholes) — unchanged
├── calendar.py          next_weekly_expiry helper — unchanged
└── sources/             Pluggable source adapters (see sources/context.md)
```

---

## `chain_collector.py`

### Responsibility

Orchestrates one chain-collection attempt per underlying per poll cycle.
Everything is isolated at the `collect_one()` boundary — the caller does not
need to know which source succeeded or failed.

### Public API

```python
async def collect_one(instrument: Instrument) -> None
    """NSE → Dhan failover for one instrument. Writes to DB, always returns."""

async def collect_tier(tier: int) -> None
    """Run collect_one() for every instrument in fno_collection_tiers at tier N."""

async def collect_all() -> None
    """Fallback: collect for every active F&O instrument, ignoring tier table."""
```

### Failover sequence

```
1. fetch from NSE
   ├── success → persist with source='nse', mark nse healthy, return
   └── SchemaError     → _record_schema_mismatch + _record_source_error
       RateLimitError  → _record_source_error
       AuthError       → _record_source_error
       SourceUnavailableError → _record_source_error
         └── 2. fetch from Dhan
               ├── success → persist with source='dhan', mark dhan healthy, return
               └── any error → _record_source_error
                     └── 3. log.status = 'missed'
```

`SchemaError` is the only exception type that writes a `chain_collection_issues`
row on top of the health error counter.  This is what drives GitHub issue filing.

### Per-poll log

Every `collect_one()` call writes exactly one `ChainCollectionLog` row
regardless of outcome:

| Field | Populated when |
|---|---|
| `primary_source` | always (`"nse"`) |
| `fallback_source` | NSE failed → `"dhan"` |
| `final_source` | NSE succeeded → `"nse"`; Dhan fallback succeeded → `"dhan"`; both failed → `None` |
| `status` | `"ok"` / `"fallback_used"` / `"missed"` |
| `nse_error` | NSE raised any exception |
| `dhan_error` | Dhan raised any exception |
| `latency_ms` | always — end-to-end wall time in ms |

### Source health helpers (private)

| Function | Effect |
|---|---|
| `_record_source_success(source)` | Resets `consecutive_errors=0`, `status='healthy'` |
| `_record_source_error(source, error)` | Increments `consecutive_errors`; flips to `'degraded'` at threshold |
| `_record_schema_mismatch(source, instrument, error, raw)` | Inserts `chain_collection_issues` row; counts recent unresolved mismatches; degrades source at threshold |
| `_persist_snapshot(snapshot, instrument, source)` | Converts `SourceSnapshot` → `ChainRow`, calls `enrich_chain_row()` for NSE strikes (Greeks absent), writes `OptionsChain` rows |

### Greek enrichment

`_persist_snapshot()` calls `chain_parser.enrich_chain_row()` for each strike
where `iv` or `delta` is `None` and the underlying/strike/time-to-expiry are all
positive.  This is always the case for NSE (no native Greeks) and never needed
for Dhan (Greeks provided natively).

### Module-level singletons

```python
_nse: NSESource = NSESource()
_dhan: DhanSource = DhanSource()
```

One instance of each source is created at import time and reused across all
collection cycles.  This is intentional — `NSESource` holds a cookie cache,
and reusing the same instance avoids unnecessary warmup GETs.

### Configuration

| Setting | Default | Effect |
|---|---|---|
| `FNO_RISK_FREE_RATE_PCT` | 6.5 | Risk-free rate used in Black-Scholes enrichment |
| `FNO_SOURCE_DEGRADE_AFTER_CONSECUTIVE_ERRORS` | 10 | Errors before source → `degraded` |
| `FNO_SOURCE_DEGRADE_AFTER_SCHEMA_ERRORS` | 3 | Schema mismatches before source → `degraded` |

---

## `tier_manager.py`

### Responsibility

Classifies every active F&O instrument into Tier 1 or Tier 2 once per day
(06:00 IST via `_fno_tier_refresh` scheduler job).  The tiers control polling
cadence: Tier 1 every 5 min, Tier 2 every 15 min.

### Classification rules

| Tier | Criteria |
|---|---|
| **Tier 1** | All 5 NSE index underlyings (`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `NIFTYNXT50`) + top-N equities by 5-day average option volume, where N = `FNO_TIER1_SIZE − 5` |
| **Tier 2** | All remaining active F&O instruments |

Index symbols are always Tier 1 regardless of volume.  Volume is computed from
`options_chain.volume` over the past 5 days — a fresh install with no history
will treat all equities as zero-volume and assign them to Tier 2 until chain
data accumulates.

### Public API

```python
async def refresh() -> dict[str, int]:
    """Recompute and upsert tier assignments. Returns {'tier1': N, 'tier2': M}."""
```

The upsert is idempotent — calling `refresh()` twice in the same day produces
the same row values and does not raise.  When a previously Tier 2 equity moves
into Tier 1 (volume-based promotion), `last_promoted_at` is updated.

### Configuration

| Setting | Default | Effect |
|---|---|---|
| `FNO_TIER1_SIZE` | 35 | Total Tier 1 slots (indices + equities) |

---

## `issue_filer.py`

### Responsibility

Aggregates the previous 24 hours of unresolved `chain_collection_issues` rows
and files (or updates) GitHub issues for each unique `(source, symbol, date)`
group.  Always sends a Telegram summary at the end.

Runs at 18:30 IST via the `_fno_issue_review_loop` scheduler job.

### Deduplication key

```
chain-issue-{source}-{symbol}-{YYYYMMDD}
```

The key is embedded as an HTML comment in every GitHub issue body:
```markdown
<!-- dedup-key: chain-issue-nse-NIFTY-20260427 -->
```

Before creating a new issue, `_search_issues()` does a GitHub issue search for
the dedup key in open issues in the configured repo.  If found:
- Age of the most recent comment is checked.
- If age > 6 hours → a new comment is added with the current failure count.
- If age ≤ 6 hours → the issue is skipped (already recently commented).

### GitHub graceful degradation

If `GITHUB_TOKEN` is empty:
- All GitHub API calls are skipped.
- A warning is logged for each group.
- The Telegram summary is still sent and includes a `⚠️ GITHUB_TOKEN not set` notice.

### Telegram summary format

```
📋 Chain Collector Review (2026-04-27)
Unresolved issue groups: N
GitHub issues created: X
GitHub issues updated: Y
[⚠️ GITHUB_TOKEN not set — no issues filed]  ← only when token missing
```

### Issue body format

Each GitHub issue includes:
- Affected source, underlying, failure count, most recent error message
- A collapsible `<details>` block with the raw response truncated to 4 KB
- The dedup key as an HTML comment

### Configuration

| Setting | Default | Effect |
|---|---|---|
| `GITHUB_REPO` | `"ashuchan/Laabh"` | Target repository for issue creation |
| `GITHUB_TOKEN` | `""` | Personal access token; empty disables GitHub |
| `GITHUB_ISSUE_LABELS` | `"bug"` | Comma-separated labels applied to new issues |
| `TELEGRAM_BOT_TOKEN` | `""` | Empty disables Telegram |
| `TELEGRAM_CHAT_ID` | `""` | Target chat for Telegram messages |

---

## Interaction diagram

```
scheduler (APScheduler)
│
├── 06:00 IST  → tier_manager.refresh()
│                  └── reads options_chain 5d avg volume
│                  └── upserts fno_collection_tiers
│
├── every 5min (09:00–15:00)  → chain_collector.collect_tier(1)
│                                └── collect_one() × Tier-1 instruments
│
├── every 15min (09:00–15:00) → chain_collector.collect_tier(2)
│                                └── collect_one() × Tier-2 instruments
│
└── 18:30 IST  → issue_filer.run()
                   └── reads chain_collection_issues (last 24h)
                   └── groups by (source, symbol, date)
                   └── GitHub create/update per group
                   └── Telegram summary
```

---

## Source health state machine

```
healthy  →  degraded  (after N consecutive errors OR M schema mismatches)
degraded →  healthy   (via POST /fno/chain-issues/{id}/resolve — last open issue cleared)
```

Neither transition happens automatically in reverse.  An operator must resolve
each open issue via the API; when the last open issue for a source is resolved,
the source flips back to `healthy`.

---

## Testing

| Test file | What it covers |
|---|---|
| `tests/test_fno_chain_failover.py` | NSE success → Dhan not called; NSE 503 → Dhan used; NSE schema error → issue logged + Dhan tried; both fail → `status='missed'`; delta parity |
| `tests/test_fno_tier_manager.py` | Tier counts with `FNO_TIER1_SIZE=35`; empty DB; idempotency; index symbols pinned to Tier 1 |
| `tests/test_fno_issue_filer.py` | Same group → one issue; rerun → no duplicate; different underlying → two issues; missing token → Telegram still fires; no issues → clean message |
| `tests/test_fno_smoke.py` | Module imports; scheduler job functions callable; config fields present |
| `tests/test_fno_integration.py` | Full parse→enrich pipeline; source field on persisted rows; latency_ms always set; collect_all iteration |
