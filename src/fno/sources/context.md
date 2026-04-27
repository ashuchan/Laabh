# `src/fno/sources/` — Chain Data Source Adapters

## Purpose

This package provides the pluggable adapter layer for fetching option chain
data.  Every concrete source implements `BaseChainSource` and returns a
`ChainSnapshot`; the collector and parser never talk to HTTP directly.

Adding a third source (e.g. a second broker) is a one-file change — implement
the abstract class and register it in `chain_collector.py`.

---

## Package layout

```
src/fno/sources/
├── __init__.py        re-exports the public surface
├── base.py            BaseChainSource ABC + ChainSnapshot / StrikeRow models
├── exceptions.py      Typed exception hierarchy
├── nse_source.py      NSE public API adapter
└── dhan_source.py     Dhan broker API adapter
```

---

## Source hierarchy

| Priority | Source | Auth | Rate limit | Greeks |
|---|---|---|---|---|
| Primary | NSE | none (cookie warmup) | `NSE_REQUEST_INTERVAL_SEC` (global semaphore) | **not provided** — computed by parser |
| Fallback | Dhan | `access-token` + `client-id` headers | `DHAN_REQUEST_INTERVAL_SEC` per symbol | **native** — passed through |

Angel One is **not** a chain source.  It was removed because it has no option
chain endpoint and its WebSocket cap (3,000 tokens) is 8× below what the full
F&O universe requires.  Angel One continues to be used for underlying ticks,
India VIX, and the per-strike Greeks API.

---

## `base.py` — contract and data models

### `BaseChainSource` (ABC)

```python
class BaseChainSource(ABC):
    name: ClassVar[str]           # 'nse' or 'dhan'

    async def fetch(self, symbol: str, expiry_date: date) -> ChainSnapshot:
        ...

    async def health_check(self) -> bool:
        ...
```

`fetch()` must either return a fully-populated `ChainSnapshot` or raise one of
the typed exceptions below.  It must never swallow errors silently.

### `ChainSnapshot`

```python
@dataclass
class ChainSnapshot:
    symbol: str
    expiry_date: date
    underlying_ltp: Decimal | None
    snapshot_at: datetime            # always UTC
    strikes: list[StrikeRow]
```

### `StrikeRow`

```python
@dataclass
class StrikeRow:
    strike: Decimal
    option_type: str                 # "CE" or "PE"
    ltp, bid, ask: Decimal | None
    bid_qty, ask_qty, volume, oi: int | None
    iv, delta, gamma, theta, vega: float | None   # optional — see Greeks section
```

---

## `exceptions.py` — typed exception hierarchy

```
ChainSourceError          base class
├── SchemaError           response shape mismatch; carries .raw_response (capped at 8 KB)
├── RateLimitError        HTTP 429 or equivalent
├── AuthError             missing / invalid / expired credentials
└── SourceUnavailableError   network error, 5xx, timeout, or any other failure
```

`SchemaError` is treated specially by `chain_collector.py`:
- A row is appended to `chain_collection_issues` with the truncated raw payload.
- After `FNO_SOURCE_DEGRADE_AFTER_SCHEMA_ERRORS` consecutive mismatches from the
  same source the source is marked `degraded` in `source_health`.

---

## `nse_source.py` — NSE adapter

### How the NSE public API works

NSE's JSON option chain endpoint is public but requires browser-like behaviour:
1. GET `https://www.nseindia.com/option-chain` to receive session cookies.
2. Subsequent GET `https://www.nseindia.com/api/option-chain-{indices|equities}?symbol=X`
   with those cookies and browser-mimicking headers.

Without step 1 (or with stale cookies) NSE returns an empty `[]` or 401/403.

### Key implementation details

| Concern | Implementation |
|---|---|
| Cookie warmup | `_refresh_cookies()` — auto-called before the first real request and when `_cookies_stale()` returns True |
| Cookie staleness | `NSE_COOKIE_REFRESH_INTERVAL_MIN` (default 5 min) |
| Auth retry | On 401/403, cookies are cleared and one refresh+retry is attempted before raising `AuthError` |
| Rate limiting | Module-level `asyncio.Semaphore(1)` + `_last_call_ts` — all calls system-wide share this budget (Tier 1 and Tier 2 jobs interleave through the same semaphore) |
| URL routing | Indices → `/api/option-chain-indices`; equities → `/api/option-chain-equities` — driven by `_INDEX_SYMBOLS` frozenset |
| Schema validation | Non-dict root, missing `records`, missing `records.data` list → `SchemaError` |
| Greeks | NSE **does not** return Greeks; they are computed by `chain_parser.enrich_chain_row()` using Black-Scholes |

### Configuration

```
NSE_USER_AGENT                  Browser UA string (rotatable if NSE blocks)
NSE_REQUEST_INTERVAL_SEC        Min seconds between any two NSE calls (default 2.5)
NSE_COOKIE_REFRESH_INTERVAL_MIN Cookie TTL in minutes (default 5)
NSE_MAX_RETRIES                 Auth refresh attempts per request (default 3)
```

---

## `dhan_source.py` — Dhan adapter

### How the Dhan API works

`POST https://api.dhan.co/v2/optionchain` with JSON body specifying the
underlying scrip, segment, and expiry.  Response includes the full chain with
bid/ask, OI, volume, IV, and all four Greeks natively.

### Key implementation details

| Concern | Implementation |
|---|---|
| Auth | `access-token` + `client-id` headers; missing → `AuthError` immediately |
| Rate limiting | Per-underlying `asyncio.Lock` + `_last_call` dict — **same symbol** calls are serialised; **different symbol** calls can run in parallel |
| Segment routing | Indices → `IDX_I`; equities → `NSE_FNO` |
| Greeks | Dhan provides `implied_volatility`, `delta`, `gamma`, `theta`, `vega` natively; the parser passes them through unchanged |
| Schema validation | Missing `data`, missing `data.oc` dict → `SchemaError` |

### Configuration

```
DHAN_CLIENT_ID                  Dhan client identifier
DHAN_ACCESS_TOKEN               Dhan OAuth bearer token
DHAN_REQUEST_INTERVAL_SEC       Min seconds per underlying per call (default 3.0)
```

---

## Greeks handling — NSE vs Dhan

| Source | Provides Greeks? | Parser action |
|---|---|---|
| NSE | No | `enrich_chain_row(row, T, r=FNO_RISK_FREE_RATE_PCT/100)` computes IV from mid-price (or LTP), then Delta/Gamma/Theta/Vega from Black-Scholes |
| Dhan | Yes (all four + IV) | Pass through unchanged |

The two sources should agree on ATM delta within ±0.02 absolute (verified by
`test_fno_integration.py::test_nse_dhan_delta_parity_within_tolerance`).

---

## Error handling summary

```
fetch() raises          Collector action
──────────────────────  ─────────────────────────────────────────────────────
SchemaError             record_schema_mismatch → chain_collection_issues row
                        record_source_error    → consecutive_errors++
                        → try fallback source
RateLimitError          record_source_error    → consecutive_errors++
                        → try fallback source
AuthError               record_source_error    → consecutive_errors++
                        → try fallback source
SourceUnavailableError  record_source_error    → consecutive_errors++
                        → try fallback source
```

After `FNO_SOURCE_DEGRADE_AFTER_CONSECUTIVE_ERRORS` (default 10) errors from
any cause, `source_health.status` flips to `degraded`.

After `FNO_SOURCE_DEGRADE_AFTER_SCHEMA_ERRORS` (default 3) schema mismatches
specifically, `source_health.status` flips to `degraded`.

Degraded sources are not automatically re-enabled.  An operator must call
`POST /fno/chain-issues/{id}/resolve` for each open issue; the last resolution
flips the source back to `healthy`.

---

## Testing

| Test file | What it covers |
|---|---|
| `tests/test_fno_chain_nse.py` | NSE URL routing, payload parsing, cookie warmup, auth retry, health_check |
| `tests/test_fno_chain_dhan.py` | Segment routing, auth header guard, parsing, Greeks pass-through, per-symbol token bucket, HTTP error mapping |
| `tests/test_fno_chain_failover.py` | NSE success → Dhan not called; NSE 503 → Dhan called once; both fail → `status='missed'`; NSE schema error → issue row created; NSE/Dhan delta parity |
| `tests/test_fno_smoke.py` | Import, instantiation, config fields, exception shapes |
| `tests/test_fno_integration.py` | Full parse→enrich pipeline; source field on persisted rows; latency_ms always set |
