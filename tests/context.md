# `tests/` — F&O Test Suite Guide

## Overview

The test suite has two layers: pre-existing tests that cover price collection,
signals, and RSS ingestion; and the NSE-primary retrofit tests added in the
chain ingestion work.  This document focuses on the retrofit tests.

**Total test count after retrofit:** 431 pass, 2 skipped  
**New tests added:** 111 across 8 test files

---

## Test layer hierarchy

```
Smoke tests        — import + instantiate + config (no I/O)
  ↓
Unit tests         — one module, mocked dependencies
  ↓
Integration tests  — multiple modules wired together, mocked HTTP + DB
  ↓
E2E tests          — full HTTP round-trip via FastAPI TestClient
```

---

## File map

| File | Layer | Tests | What it covers |
|---|---|---|---|
| `test_fno_smoke.py` | Smoke | 45 (2 skip) | Imports, exception classes, data models, source instantiation, ORM models, config fields, migration file, scheduler jobs, Pydantic schemas |
| `test_fno_chain_nse.py` | Unit | ~15 | NSE URL routing, response parsing, cookie warmup, auth retry, `health_check` |
| `test_fno_chain_dhan.py` | Unit | ~15 | Segment routing, auth header guard, response parsing, native Greek pass-through, per-symbol token bucket, HTTP error code mapping |
| `test_fno_chain_failover.py` | Unit/Integration | 5 | Failover state machine: NSE success, NSE 503, NSE schema error, both fail, NSE/Dhan delta parity |
| `test_fno_tier_manager.py` | Integration | 4 | Tier counts, empty DB, idempotency, index symbols pinned to Tier 1 |
| `test_fno_issue_filer.py` | Integration | 5 | Dedup (same group → one issue), rerun idempotency, different underlying → two issues, missing token → Telegram fires, no issues → clean message |
| `test_fno_integration.py` | Integration | 33 | NSE parse→enrich pipeline, Dhan Greek pass-through, delta parity, tier logic, dedup key format, source health transitions, OptionsChain source field, latency_ms, URL routing (parametrized), collect_all |
| `test_fno_e2e.py` | E2E | 33 | All 3 new API endpoints; regression checks for existing endpoints; route registration |

---

## Smoke tests — detailed guide (`test_fno_smoke.py`)

Smoke tests are the fastest safety net: they exercise nothing but Python-level
imports, object construction, and attribute access.  No network calls, no DB,
no async event loop required.  If any of these fail, the module is broken at
the most fundamental level.

### Why smoke tests matter here

The retrofit added 14 new source files and ORM models.  A broken import (missing
dependency, circular import, typo in `__all__`) would silently make every other
test fail with an unrelated `ImportError`.  Smoke tests make that failure
immediate and obvious.

### Section-by-section breakdown

#### Module importability (13 tests)

```python
@pytest.mark.parametrize("module_path", [
    "src.fno.sources.exceptions",
    "src.fno.sources.base",
    "src.fno.sources.nse_source",
    "src.fno.sources.dhan_source",
    "src.fno.sources",            # __init__.py re-exports
    "src.fno.chain_collector",
    "src.fno.tier_manager",
    "src.fno.issue_filer",
    "src.models.fno_collection_tier",
    "src.models.fno_chain_log",
    "src.models.fno_chain_issue",
    "src.models.fno_source_health",
    "src.models.fno_chain",
])
def test_module_imports(module_path: str) -> None:
```

Each parametrized case uses `importlib.import_module()` so a failure reports
the exact module path.  This also verifies that `src/fno/sources/__init__.py`
re-exports correctly.

#### Exception classes (6 tests)

```
test_chain_source_error_base           — ChainSourceError is a plain Exception subclass
test_schema_error_carries_raw_response — raw_response is stored on the exception object
test_schema_error_truncates_raw_to_8kb — raw > 8192 chars is capped at exactly 8192
test_rate_limit_error                  — RateLimitError is raise-able
test_auth_error                        — AuthError is an Exception
test_source_unavailable_error          — SourceUnavailableError is an Exception
```

The 8 KB cap is critical: `SchemaError.raw_response` is persisted to
`chain_collection_issues.raw_response` and passed to GitHub issue bodies.
The smoke test confirms the cap is applied at construction time, not at write
time.

```python
def test_schema_error_truncates_raw_to_8kb() -> None:
    raw = "x" * 20000
    err = SchemaError("too long", raw)
    assert len(err.raw_response) == 8192   # exactly 8192, not 8191 or 8193
```

#### BaseChainSource data models (4 tests)

```
test_strike_row_defaults              — all optional fields default to None
test_strike_row_full_fields           — all fields can be set; delta accessible
test_chain_snapshot_ce_pe_filtering   — ce_strikes() / pe_strikes() filter correctly
test_chain_snapshot_empty_strikes     — no strikes → ce_strikes() == pe_strikes() == []
```

`StrikeRow` has 14 fields, 12 of which are optional.  The default test confirms
that constructing with only `strike` and `option_type` doesn't raise and leaves
all Greeks as `None`.

The filter test verifies the convenience methods on `ChainSnapshot`:

```python
snap = ChainSnapshot(symbol="NIFTY", ..., strikes=[
    StrikeRow(strike=Decimal("22000"), option_type="CE"),
    StrikeRow(strike=Decimal("22000"), option_type="PE"),
    StrikeRow(strike=Decimal("21900"), option_type="CE"),
])
assert len(snap.ce_strikes()) == 2
assert len(snap.pe_strikes()) == 1
```

#### NSESource instantiation (3 tests)

```
test_nse_source_instantiates         — src.name == 'nse'; src._cookies == {}
test_nse_source_cookies_stale_when_empty — _cookies_stale() is True on fresh instance
test_nse_source_builds_headers       — _build_headers() returns dict with User-Agent, Referer, Accept
```

These verify the contract that `NSESource()` requires no arguments and starts
with no cookies (triggering warmup on first fetch).

#### DhanSource instantiation (2 tests)

```
test_dhan_source_instantiates        — src.name == 'dhan'
test_dhan_source_segment_routing     — NIFTY → _SEG_INDEX; RELIANCE → _SEG_EQUITY
```

Segment routing is tested here rather than in unit tests because it requires no
async context — `_segment_for()` is a synchronous method that uses a frozenset.

#### ORM models (5 tests)

```
test_fno_collection_tier_model       — FNOCollectionTier(instrument_id=..., tier=1, avg_volume_5d=500_000)
test_chain_collection_log_model      — ChainCollectionLog with primary_source='nse', status='ok'
test_chain_collection_issue_model    — ChainCollectionIssue; resolved_at is None by default
test_source_health_model             — SourceHealth(source='nse', status='healthy')
test_options_chain_has_source_column — OptionsChain.source attribute exists (added in migration 0005)
```

**Important nuance for `test_source_health_model`:**

```python
def test_source_health_model() -> None:
    row = SourceHealth(source="nse", status="healthy")
    assert row.status == "healthy"
    # SQLAlchemy column defaults are applied at INSERT time, not on Python object creation
    assert row.consecutive_errors in (0, None)
```

`consecutive_errors` has `default=0` in the SQLAlchemy column definition.
That default is applied by the DB on `INSERT`, not when the Python object is
constructed.  An in-memory `SourceHealth()` object may have `consecutive_errors
== None` or `== 0` depending on SQLAlchemy version.  The test accepts either.

#### Configuration fields (5 tests)

```
test_config_nse_fields              — nse_user_agent (len>10), nse_request_interval_sec=2.5,
                                      nse_cookie_refresh_interval_min=5, nse_max_retries=3
test_config_dhan_fields             — dhan_client_id='', dhan_access_token='', dhan_request_interval_sec=3.0
test_config_github_fields           — github_repo='ashuchan/Laabh', github_token='', 'bug' in labels
test_config_tier_policy_fields      — fno_tier1_size=35, cadence_min 5 and 15
test_config_source_health_policy_fields — degrade_after_schema_errors=3, consecutive_errors=10
test_config_nse_primary_flag_defaults_true — fno_chain_nse_primary is True
test_config_risk_free_rate          — fno_risk_free_rate_pct == 6.5
```

All new config fields use sensible defaults so the system works out-of-the-box
with no `.env` changes.  These tests act as a regression guard — if a default
changes, the test fails and forces a deliberate decision.

#### Migration file (2 tests)

```
test_migration_0005_revision_id         — revision == '0005_add_chain_ingestion_observability'
                                          down_revision == '0004_fno_intelligence_module'
test_migration_0005_has_upgrade_and_downgrade — both callable
```

The migration file is loaded via `importlib.util.spec_from_file_location()`
because its filename starts with a digit (`0005_...`) and Python cannot import
such a file using the normal import system:

```python
def _load_migration_0005():
    import importlib.util, pathlib
    mig_path = pathlib.Path(__file__).parent.parent / \
        "database/migrations/versions/0005_add_chain_ingestion_observability.py"
    spec = importlib.util.spec_from_file_location("mig_0005", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
```

#### Scheduler jobs (2 tests, may skip)

```
test_scheduler_has_new_fno_job_functions  — _fno_chain_collect_tier1/tier2,
                                            _fno_tier_refresh, _fno_issue_review_loop callable
test_scheduler_no_longer_has_old_chain_refresh — _fno_chain_refresh does not exist
```

Both tests wrap the scheduler import in `try/except ModuleNotFoundError` with
`pytest.skip()`.  The scheduler imports `feedparser` transitively; `feedparser`
requires `sgmllib3k` which fails to build its wheel on Python 3.11.  The skip
guard prevents this environment-specific failure from blocking the whole suite.

```python
def test_scheduler_has_new_fno_job_functions() -> None:
    try:
        import src.scheduler as sched
    except ModuleNotFoundError as exc:
        pytest.skip(f"scheduler dependency missing: {exc}")
    assert callable(sched._fno_chain_collect_tier1)
    ...
```

#### Pydantic API schemas (3 tests)

```
test_chain_issue_response_schema    — ChainIssueResponse(source, issue_type, ...); resolved_at=None
test_resolve_issue_response_schema  — ResolveIssueResponse(resolved=True, source_health_status='healthy')
test_source_health_response_schema  — SourceHealthResponse(source='nse', status='healthy', consecutive_errors=0)
```

These confirm that the Pydantic models validate without error and expose the
expected fields with correct types before the E2E tests exercise them through
HTTP.

---

## Running the tests

```bash
# All retrofit tests
pytest tests/test_fno_smoke.py tests/test_fno_chain_nse.py tests/test_fno_chain_dhan.py \
       tests/test_fno_chain_failover.py tests/test_fno_tier_manager.py \
       tests/test_fno_issue_filer.py tests/test_fno_integration.py tests/test_fno_e2e.py -v

# Smoke only (fastest — no async, no mocks)
pytest tests/test_fno_smoke.py -v

# Async tests only
pytest tests/test_fno_chain_failover.py tests/test_fno_tier_manager.py \
       tests/test_fno_issue_filer.py tests/test_fno_integration.py -v

# E2E only
pytest tests/test_fno_e2e.py -v

# Full suite
pytest
```

---

## Mocking patterns used across the retrofit tests

### Patching module-level source singletons

`chain_collector.py` creates `_nse` and `_dhan` at import time.  Tests that
exercise the failover logic must patch these by name:

```python
# IMPORTANT: chain_collector must be imported before the patch resolves
import src.fno.chain_collector  # noqa: F401

with (
    patch("src.fno.chain_collector._nse") as mock_nse,
    patch("src.fno.chain_collector._dhan") as mock_dhan,
):
    mock_nse.fetch = AsyncMock(return_value=snapshot)
```

Without the top-level import, `patch()` cannot resolve `src.fno.chain_collector`
as a module attribute and raises `AttributeError`.

### Async session context manager

```python
def _mock_session():
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(...))
    mock_session.add = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx

# Used as:
patch("src.fno.chain_collector.session_scope", return_value=_mock_session())
```

Integration tests use `@asynccontextmanager` directly for finer control:

```python
@asynccontextmanager
async def _scope():
    yield mock_session

with patch("src.fno.chain_collector.session_scope", _scope):
    ...
```

### FastAPI TestClient with patched session

E2E tests build a minimal FastAPI app with just the F&O router, then patch
`session_scope` at the route level:

```python
_app = FastAPI()
_app.include_router(router)
_client = TestClient(_app, raise_server_exceptions=True)

def test_something():
    sess = _make_session(execute_rows=[...])
    with patch("src.api.routes.fno.session_scope", _scope(sess)):
        resp = _client.get("/fno/chain-issues")
    assert resp.status_code == 200
```

---

## Common failure modes and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `AttributeError: module 'src.fno' has no attribute 'chain_collector'` | `patch()` ran before `chain_collector` was imported | Add `import src.fno.chain_collector` at top of test file |
| `AssertionError: assert None in (0, None)` on `consecutive_errors` | SQLAlchemy column default applied at INSERT, not construction | Use `assert row.consecutive_errors in (0, None)` |
| `ModuleNotFoundError: No module named 'sgmllib3k'` in scheduler tests | `feedparser` wheel fails on Python 3.11 | Use `pytest.skip()` guard around `import src.scheduler` |
| `ImportError` when loading migration module | Filename starts with digit — can't use `import` | Use `importlib.util.spec_from_file_location()` |
| Async test hangs or `ScopeMismatch` | Missing `@pytest.mark.asyncio` | Add decorator; verify `pytest-asyncio` is installed |
