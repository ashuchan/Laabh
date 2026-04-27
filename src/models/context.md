# `src/models/` — SQLAlchemy ORM Models

## Purpose

Each file in this package maps a PostgreSQL table to a SQLAlchemy declarative
model.  Models are thin data containers — no business logic lives here.

The four models added in migration `0005_add_chain_ingestion_observability`
support the NSE-primary chain ingestion retrofit and are documented in detail
below.  The pre-existing models (`Instrument`, `OptionsChain`, `price`, etc.)
are covered by the main schema docs.

---

## Added in migration 0005

### `fno_collection_tier.py` → `FNOCollectionTier`

**Table:** `fno_collection_tiers`

One row per active F&O instrument.  Written by `tier_manager.refresh()` every
morning at 06:00 IST.  Read by `chain_collector.collect_tier()` to decide which
instruments to poll at what cadence.

```python
class FNOCollectionTier(Base):
    instrument_id: Mapped[uuid.UUID]    # PK, FK → instruments.id
    tier: Mapped[int]                   # CHECK IN (1, 2)
    avg_volume_5d: Mapped[int | None]   # 5-day avg option volume from options_chain
    last_promoted_at: Mapped[datetime | None]  # set when tier 2 → tier 1
    updated_at: Mapped[datetime | None]
```

Key notes:
- `instrument_id` is the primary key — one row per instrument, no history kept.
- `tier` is constrained to `1` or `2` by a `CHECK` constraint.
- `avg_volume_5d` is `None` for instruments with no recent chain data (treated
  as zero-volume for sorting purposes).
- `last_promoted_at` is only updated when a Tier 2 equity moves into Tier 1;
  stays `None` for instruments that started in Tier 1 (indices).

---

### `fno_chain_log.py` → `ChainCollectionLog`

**Table:** `chain_collection_log`

One row per instrument per poll cycle.  Written by `chain_collector.collect_one()`
at the end of every attempt, regardless of outcome.  This is the primary audit
trail for chain ingestion.

```python
class ChainCollectionLog(Base):
    id: Mapped[uuid.UUID]               # PK
    instrument_id: Mapped[uuid.UUID]    # FK → instruments.id
    attempted_at: Mapped[datetime]      # UTC timestamp of poll start
    primary_source: Mapped[str]         # always "nse"
    fallback_source: Mapped[str | None] # "dhan" when NSE failed, else None
    final_source: Mapped[str | None]    # "nse" / "dhan" / None (missed)
    status: Mapped[str]                 # CHECK IN ('ok','fallback_used','missed')
    nse_error: Mapped[str | None]       # error string when NSE raised
    dhan_error: Mapped[str | None]      # error string when Dhan raised
    latency_ms: Mapped[int | None]      # total wall time in milliseconds
```

Status meanings:

| `status` | Meaning |
|---|---|
| `ok` | NSE succeeded |
| `fallback_used` | NSE failed, Dhan succeeded |
| `missed` | Both sources failed; no data written to `options_chain` |

`latency_ms` is always populated (it covers the full `collect_one()` wall time,
including any fallback attempt).  `nse_error` and `dhan_error` are only set when
the respective source raised an exception; they do not appear together unless
both sources failed.

---

### `fno_chain_issue.py` → `ChainCollectionIssue`

**Table:** `chain_collection_issues`

Tracks schema mismatches and sustained failures that rise to the level of needing
human attention.  Written by `_record_schema_mismatch()` in `chain_collector.py`.
Read by `issue_filer.run()` to aggregate and file GitHub issues.

```python
class ChainCollectionIssue(Base):
    id: Mapped[uuid.UUID]               # PK
    source: Mapped[str]                 # "nse" or "dhan"
    instrument_id: Mapped[uuid.UUID | None]  # FK → instruments.id (optional)
    issue_type: Mapped[str]             # CHECK IN ('schema_mismatch','sustained_failure','auth_error')
    error_message: Mapped[str]          # human-readable description
    raw_response: Mapped[str | None]    # truncated to 8 KB — the payload that caused the error
    detected_at: Mapped[datetime | None]
    github_issue_url: Mapped[str | None]  # backfilled by issue_filer after issue creation
    resolved_at: Mapped[datetime | None]  # set by POST /fno/chain-issues/{id}/resolve
    resolved_by: Mapped[str | None]       # operator identifier (free text)
```

Key notes:
- `raw_response` is capped at 8 KB — the same limit enforced by `SchemaError`.
- `github_issue_url` starts `None` and is backfilled when `issue_filer.run()` creates
  or finds the corresponding GitHub issue.
- `resolved_at` being `None` means the issue is still open.  The API endpoint
  `POST /fno/chain-issues/{id}/resolve` sets `resolved_at` and, when the last
  open issue for a source is resolved, flips `source_health.status` to `'healthy'`.
- `issue_type` is constrained by a `CHECK` constraint.  Only `schema_mismatch` is
  currently written automatically; `sustained_failure` and `auth_error` are reserved
  for future use or manual insertion.

---

### `fno_source_health.py` → `SourceHealth`

**Table:** `source_health`

One row per data source, seeded at migration time with `status='healthy'` for
`nse`, `dhan`, and `angel_one`.  Acts as the circuit-breaker state.

```python
class SourceHealth(Base):
    source: Mapped[str]                  # PK: 'nse', 'dhan', 'angel_one'
    status: Mapped[str]                  # CHECK IN ('healthy','degraded','failed')
    consecutive_errors: Mapped[int]      # reset to 0 on any success; incremented on any error
    last_success_at: Mapped[datetime | None]
    last_error_at: Mapped[datetime | None]
    last_error: Mapped[str | None]       # last error message, capped at 500 chars
    updated_at: Mapped[datetime | None]
```

State machine:

```
healthy  ──(N consecutive errors OR M schema mismatches)──▶  degraded
degraded ──(last open issue resolved via API)──────────────▶  healthy
```

`N` = `FNO_SOURCE_DEGRADE_AFTER_CONSECUTIVE_ERRORS` (default 10)
`M` = `FNO_SOURCE_DEGRADE_AFTER_SCHEMA_ERRORS` (default 3)

`failed` is a reserved terminal state for future use (e.g. auth permanently
invalidated).  It is not currently set by any code path.

Important: `consecutive_errors` and `status` are SQLAlchemy column `default=`
values, which means they are applied at `INSERT` time (when the session flushes
to the database), not when a Python `SourceHealth()` object is constructed in
memory.  In-memory, the field may read as `None` until it is flushed.

---

## `fno_chain.py` — change in migration 0005

Migration 0005 adds a `source` column to the pre-existing `options_chain` table:

```python
# In OptionsChain:
source: Mapped[str | None] = mapped_column(String(20), default="nse")
```

This records which data source provided each chain row.  Every row written by
`chain_collector._persist_snapshot()` sets this to `"nse"` or `"dhan"`.
Rows written before migration 0005 will have `source = NULL`.

---

## Testing

All four new models are covered by `tests/test_fno_smoke.py`:
- Instantiation without a DB session (verifying column declarations)
- `SourceHealth.consecutive_errors in (0, None)` acknowledges the flush-time default
- `OptionsChain.source` attribute existence check

The full observability pipeline (write → read → assert) is tested in
`tests/test_fno_chain_failover.py` (ChainCollectionLog status='missed' with
both error fields populated) and `tests/test_fno_integration.py` (source field
on persisted OptionsChain rows, latency_ms always set).
