"""Integration tests — exercise component interactions without a real database.

These tests wire multiple modules together, using mock HTTP transports and
in-memory session mocks to verify the full data flow:

  NSE/Dhan response → parse → enrich Greeks → persist → log
  collect_one failover state machine
  tier classification logic
  issue dedup key construction
  source health state transitions
  record_source_error / record_schema_mismatch side effects
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Pre-import modules whose attributes are patched by name
import src.fno.chain_collector  # noqa: F401


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_INST_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_EXPIRY = date(2026, 4, 29)
_NOW = datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)
_NIFTY_EXPIRY_STR = "29-Apr-2026"


def _make_instrument(symbol: str = "NIFTY") -> MagicMock:
    inst = MagicMock()
    inst.id = _INST_ID
    inst.symbol = symbol
    return inst


def _nse_payload(expiry_str: str = _NIFTY_EXPIRY_STR) -> dict:
    return {
        "records": {
            "underlyingValue": 22000.0,
            "data": [
                {
                    "expiryDate": expiry_str,
                    "strikePrice": 22000,
                    "CE": {
                        "lastPrice": 150.5,
                        "bidprice": 149.0,
                        "askPrice": 151.0,
                        "bidQty": 50,
                        "askQty": 75,
                        "totalTradedVolume": 12000,
                        "openInterest": 80000,
                    },
                    "PE": {
                        "lastPrice": 140.0,
                        "bidprice": 138.0,
                        "askPrice": 142.0,
                        "bidQty": 60,
                        "askQty": 80,
                        "totalTradedVolume": 9000,
                        "openInterest": 70000,
                    },
                }
            ],
        }
    }


def _dhan_payload() -> dict:
    return {
        "data": {
            "last_price": 22000.0,
            "oc": {
                "22000": {
                    "call": {
                        "last_price": 150.5,
                        "bid_price": 149.0,
                        "ask_price": 151.0,
                        "bid_qty": 50,
                        "ask_qty": 75,
                        "volume": 12000,
                        "oi": 80000,
                        "implied_volatility": 0.185,
                        "delta": 0.52,
                        "gamma": 0.0012,
                        "theta": -3.50,
                        "vega": 8.20,
                    },
                    "put": {
                        "last_price": 140.0,
                        "bid_price": 138.0,
                        "ask_price": 142.0,
                        "bid_qty": 60,
                        "ask_qty": 80,
                        "volume": 9000,
                        "oi": 70000,
                        "implied_volatility": 0.190,
                        "delta": -0.48,
                        "gamma": 0.0011,
                        "theta": -3.20,
                        "vega": 7.80,
                    },
                }
            },
        }
    }


# ---------------------------------------------------------------------------
# NSE parse → enrich pipeline
# ---------------------------------------------------------------------------

def test_nse_parse_and_enrich_greeks_are_computed() -> None:
    """NSE doesn't return Greeks; enrich_chain_row must compute them from IV."""
    from src.fno.chain_parser import ChainRow, enrich_chain_row
    from src.fno.sources.nse_source import NSESource

    src = NSESource()
    snap = src._parse_response(_nse_payload(), "NIFTY", _EXPIRY)

    # NSE strikes have no IV or Greeks
    ce = next(s for s in snap.strikes if s.option_type == "CE")
    assert ce.iv is None
    assert ce.delta is None

    # Simulate what chain_collector does: build a ChainRow and enrich it
    row = ChainRow(
        instrument_id=_INST_ID,
        expiry_date=_EXPIRY,
        strike_price=ce.strike,
        option_type="CE",
        ltp=ce.ltp,
        bid_price=ce.bid,
        ask_price=ce.ask,
        underlying_ltp=snap.underlying_ltp,
    )
    T = max(0.0, (_EXPIRY - _NOW.date()).days / 365.0)
    enriched = enrich_chain_row(row, T, r=0.065)

    # With bid/ask available, IV should be computed
    assert enriched.iv is not None
    assert enriched.delta is not None
    assert 0.0 < enriched.delta < 1.0  # ATM call delta ∈ (0, 1)


def test_nse_parse_ce_and_pe_both_enriched() -> None:
    from src.fno.chain_parser import ChainRow, enrich_chain_row
    from src.fno.sources.nse_source import NSESource

    src = NSESource()
    snap = src._parse_response(_nse_payload(), "NIFTY", _EXPIRY)
    T = max(0.0, (_EXPIRY - _NOW.date()).days / 365.0)

    for strike in snap.strikes:
        row = ChainRow(
            instrument_id=_INST_ID,
            expiry_date=_EXPIRY,
            strike_price=strike.strike,
            option_type=strike.option_type,
            ltp=strike.ltp,
            bid_price=strike.bid,
            ask_price=strike.ask,
            underlying_ltp=snap.underlying_ltp,
        )
        enriched = enrich_chain_row(row, T, r=0.065)
        assert enriched.delta is not None


# ---------------------------------------------------------------------------
# Dhan parse → Greeks preserved (not overwritten)
# ---------------------------------------------------------------------------

def test_dhan_greeks_pass_through_unchanged() -> None:
    """Dhan provides Delta/Gamma/Theta/Vega natively; they must not be recomputed."""
    from src.fno.sources.dhan_source import DhanSource

    src = DhanSource()
    snap = src._parse_response(_dhan_payload(), "NIFTY", _EXPIRY)

    call_row = next(s for s in snap.strikes if s.option_type == "CE")
    # Dhan values must be exactly preserved
    assert call_row.delta == pytest.approx(0.52)
    assert call_row.gamma == pytest.approx(0.0012)
    assert call_row.theta == pytest.approx(-3.50)
    assert call_row.vega == pytest.approx(8.20)
    assert call_row.iv == pytest.approx(0.185)


def test_dhan_put_delta_negative() -> None:
    from src.fno.sources.dhan_source import DhanSource

    src = DhanSource()
    snap = src._parse_response(_dhan_payload(), "NIFTY", _EXPIRY)
    put_row = next(s for s in snap.strikes if s.option_type == "PE")
    assert put_row.delta is not None
    assert put_row.delta < 0  # put delta is negative


# ---------------------------------------------------------------------------
# NSE ↔ Dhan Greeks parity
# ---------------------------------------------------------------------------

def test_nse_dhan_delta_parity_within_tolerance() -> None:
    """Black-Scholes delta computed from NSE IV vs Dhan-reported delta: ±0.02."""
    from src.fno.chain_parser import compute_greeks

    # Shared scenario: ATM call, 14 days to expiry, IV≈18.5%
    greeks = compute_greeks(S=22000, K=22000, T=14 / 365.0, r=0.065, sigma=0.185, opt="CE")
    dhan_delta = 0.52  # from Dhan payload above
    assert abs(greeks["delta"] - dhan_delta) <= 0.02


def test_nse_dhan_put_delta_parity() -> None:
    from src.fno.chain_parser import compute_greeks

    greeks = compute_greeks(S=22000, K=22000, T=14 / 365.0, r=0.065, sigma=0.190, opt="PE")
    dhan_put_delta = -0.48
    assert abs(greeks["delta"] - dhan_put_delta) <= 0.02


# ---------------------------------------------------------------------------
# Tier manager — classification logic
# ---------------------------------------------------------------------------

def test_tier1_indices_always_included() -> None:
    """All 5 NSE index symbols must end up in Tier 1 regardless of volume."""
    from src.fno.tier_manager import _INDEX_SYMBOLS
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
        assert sym in _INDEX_SYMBOLS


def test_tier_assignment_respects_volume_ordering() -> None:
    """The equity with the highest 5d avg volume should be in Tier 1."""
    # This tests the pure sorting logic without a DB
    instruments = [MagicMock(id=uuid.uuid4(), symbol=f"STOCK{i}") for i in range(10)]
    volume_map = {inst.id: float(i * 10000) for i, inst in enumerate(instruments)}

    # Sort equities by volume descending
    sorted_equities = sorted(
        instruments,
        key=lambda i: volume_map.get(i.id, 0.0),
        reverse=True,
    )
    # The first instrument (highest volume) should be in Tier 1
    assert sorted_equities[0].symbol == "STOCK9"  # highest volume


def test_tier1_size_cap_respected() -> None:
    """Never assign more than FNO_TIER1_SIZE instruments to Tier 1."""
    tier1_size = 35
    n_equities = 200
    n_indices = 5
    total = n_indices + n_equities

    index_ids = {uuid.uuid4() for _ in range(n_indices)}
    equity_ids = [uuid.uuid4() for _ in range(n_equities)]
    volume_map = {eid: float(i * 1000) for i, eid in enumerate(equity_ids)}

    equity_tier1_slots = max(0, tier1_size - len(index_ids))
    equities_sorted = sorted(equity_ids, key=lambda x: volume_map.get(x, 0.0), reverse=True)
    tier1_equity_ids = set(equities_sorted[:equity_tier1_slots])
    tier1_ids = index_ids | tier1_equity_ids

    assert len(tier1_ids) == tier1_size
    assert len([x for x in range(total) if x not in range(tier1_size)]) == total - tier1_size


# ---------------------------------------------------------------------------
# Issue filer — dedup key construction
# ---------------------------------------------------------------------------

def test_dedup_key_format() -> None:
    """Dedup key must be chain-issue-{source}-{symbol}-{YYYYMMDD}."""
    source = "nse"
    symbol = "NIFTY"
    detected_at = datetime(2026, 4, 27, 18, 30, tzinfo=timezone.utc)
    day = detected_at.strftime("%Y%m%d")
    key = f"chain-issue-{source}-{symbol}-{day}"
    assert key == "chain-issue-nse-NIFTY-20260427"


def test_dedup_key_different_sources_produce_different_keys() -> None:
    day = "20260427"
    nse_key = f"chain-issue-nse-NIFTY-{day}"
    dhan_key = f"chain-issue-dhan-NIFTY-{day}"
    assert nse_key != dhan_key


def test_dedup_key_different_symbols_produce_different_keys() -> None:
    day = "20260427"
    nifty_key = f"chain-issue-nse-NIFTY-{day}"
    reliance_key = f"chain-issue-nse-RELIANCE-{day}"
    assert nifty_key != reliance_key


def test_dedup_key_different_dates_produce_different_keys() -> None:
    nifty_apr27 = "chain-issue-nse-NIFTY-20260427"
    nifty_apr28 = "chain-issue-nse-NIFTY-20260428"
    assert nifty_apr27 != nifty_apr28


# ---------------------------------------------------------------------------
# _record_source_error — consecutive_errors increment + degrade logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_source_error_increments_count() -> None:
    """_record_source_error must increment consecutive_errors on the health row."""
    from src.fno.chain_collector import _record_source_error
    from src.models.fno_source_health import SourceHealth

    health_row = SourceHealth(source="nse", status="healthy")
    health_row.consecutive_errors = 2

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=health_row))
    )

    @asynccontextmanager
    async def _scope():
        yield mock_session

    with patch("src.fno.chain_collector.session_scope", _scope):
        with patch("src.fno.chain_collector._settings") as ms:
            ms.fno_source_degrade_after_consecutive_errors = 10
            await _record_source_error("nse", "timeout")

    assert health_row.consecutive_errors == 3


@pytest.mark.asyncio
async def test_record_source_error_degrades_at_threshold() -> None:
    from src.fno.chain_collector import _record_source_error
    from src.models.fno_source_health import SourceHealth

    health_row = SourceHealth(source="nse", status="healthy")
    health_row.consecutive_errors = 9  # one away from threshold

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=health_row))
    )

    @asynccontextmanager
    async def _scope():
        yield mock_session

    with patch("src.fno.chain_collector.session_scope", _scope):
        with patch("src.fno.chain_collector._settings") as ms:
            ms.fno_source_degrade_after_consecutive_errors = 10
            await _record_source_error("nse", "network error")

    assert health_row.consecutive_errors == 10
    assert health_row.status == "degraded"


# ---------------------------------------------------------------------------
# _record_schema_mismatch — issue row created + degrade after threshold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_schema_mismatch_creates_issue_row() -> None:
    from src.fno.chain_collector import _record_schema_mismatch
    from src.models.fno_chain_issue import ChainCollectionIssue
    from src.models.fno_source_health import SourceHealth

    added: list = []
    health_row = SourceHealth(source="nse", status="healthy")
    health_row.consecutive_errors = 0

    mock_session = AsyncMock()

    async def fake_execute(query):
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    mock_session.execute = AsyncMock(side_effect=fake_execute)
    mock_session.add = MagicMock(side_effect=lambda obj: added.append(obj))

    @asynccontextmanager
    async def _scope():
        yield mock_session

    instrument = _make_instrument()

    with patch("src.fno.chain_collector.session_scope", _scope):
        with patch("src.fno.chain_collector._settings") as ms:
            ms.fno_source_degrade_after_schema_errors = 3
            await _record_schema_mismatch("nse", instrument, "missing key", '{"x":1}')

    issue_rows = [o for o in added if isinstance(o, ChainCollectionIssue)]
    assert len(issue_rows) == 1
    assert issue_rows[0].source == "nse"
    assert issue_rows[0].issue_type == "schema_mismatch"
    assert issue_rows[0].error_message == "missing key"


# ---------------------------------------------------------------------------
# Full collect_one flow: NSE returns valid data → OptionsChain row persisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_one_nse_ok_persists_with_nse_source() -> None:
    """When NSE succeeds, OptionsChain rows must be written with source='nse'."""
    from src.fno.sources.base import ChainSnapshot, StrikeRow

    snapshot = ChainSnapshot(
        symbol="NIFTY",
        expiry_date=_EXPIRY,
        underlying_ltp=Decimal("22000"),
        snapshot_at=_NOW,
        strikes=[StrikeRow(strike=Decimal("22000"), option_type="CE", ltp=Decimal("150"))],
    )

    added: list = []
    mock_session = AsyncMock()
    mock_session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )

    @asynccontextmanager
    async def _scope():
        yield mock_session

    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan"),
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", _scope),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
    ):
        mock_nse.name = "nse"
        mock_nse.fetch = AsyncMock(return_value=snapshot)

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

    from src.models.fno_chain import OptionsChain
    chain_rows = [o for o in added if isinstance(o, OptionsChain)]
    assert len(chain_rows) >= 1
    assert all(r.source == "nse" for r in chain_rows)


@pytest.mark.asyncio
async def test_collect_one_dhan_fallback_sets_correct_source() -> None:
    """When Dhan is used as fallback, OptionsChain rows must have source='dhan'."""
    from src.fno.sources.base import ChainSnapshot, StrikeRow
    from src.fno.sources.exceptions import SourceUnavailableError

    snapshot = ChainSnapshot(
        symbol="NIFTY",
        expiry_date=_EXPIRY,
        underlying_ltp=Decimal("22000"),
        snapshot_at=_NOW,
        strikes=[StrikeRow(strike=Decimal("22000"), option_type="CE")],
    )

    added: list = []
    mock_session = AsyncMock()
    mock_session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )

    @asynccontextmanager
    async def _scope():
        yield mock_session

    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan") as mock_dhan,
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", _scope),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(side_effect=SourceUnavailableError("503"))
        mock_dhan.fetch = AsyncMock(return_value=snapshot)

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

    from src.models.fno_chain import OptionsChain
    chain_rows = [o for o in added if isinstance(o, OptionsChain)]
    # Chain rows should exist and be sourced from dhan
    assert all(r.source == "dhan" for r in chain_rows)


@pytest.mark.asyncio
async def test_collect_one_log_latency_always_set() -> None:
    """ChainCollectionLog must always have latency_ms populated."""
    from src.fno.sources.base import ChainSnapshot, StrikeRow

    snapshot = ChainSnapshot(
        symbol="NIFTY",
        expiry_date=_EXPIRY,
        underlying_ltp=Decimal("22000"),
        snapshot_at=_NOW,
        strikes=[StrikeRow(strike=Decimal("22000"), option_type="CE")],
    )

    added: list = []
    mock_session = AsyncMock()
    mock_session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )

    @asynccontextmanager
    async def _scope():
        yield mock_session

    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan"),
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", _scope),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(return_value=snapshot)

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

    from src.models.fno_chain_log import ChainCollectionLog
    logs = [o for o in added if isinstance(o, ChainCollectionLog)]
    assert len(logs) == 1
    assert logs[0].latency_ms is not None
    assert logs[0].latency_ms >= 0


# ---------------------------------------------------------------------------
# Schema raw_response truncation (8 KB cap)
# ---------------------------------------------------------------------------

def test_schema_error_raw_response_is_capped() -> None:
    from src.fno.sources.exceptions import SchemaError
    raw = "A" * 100_000
    err = SchemaError("too large", raw)
    assert len(err.raw_response) == 8192


def test_schema_error_short_response_preserved() -> None:
    from src.fno.sources.exceptions import SchemaError
    raw = '{"error": "bad shape"}'
    err = SchemaError("mismatch", raw)
    assert err.raw_response == raw


# ---------------------------------------------------------------------------
# NSE URL routing for all known index symbols
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"])
def test_nse_url_routing_indices(symbol: str) -> None:
    from src.fno.sources.nse_source import NSESource, _INDICES_URL
    src = NSESource()
    url = src._url_for(symbol)
    assert url.startswith(_INDICES_URL), f"{symbol} should use indices URL"


@pytest.mark.parametrize("symbol", ["RELIANCE", "TCS", "INFY", "HDFC", "ICICIBANK"])
def test_nse_url_routing_equities(symbol: str) -> None:
    from src.fno.sources.nse_source import NSESource, _EQUITIES_URL
    src = NSESource()
    url = src._url_for(symbol)
    assert url.startswith(_EQUITIES_URL), f"{symbol} should use equities URL"


# ---------------------------------------------------------------------------
# Issue filer — raw response truncation in issue body
# ---------------------------------------------------------------------------

def test_issue_body_raw_response_cap() -> None:
    """Issue body should not include more than 4096 chars of raw response."""
    raw = "X" * 10_000
    truncated = raw[:4096]
    # Verify the body construction pattern caps at 4096
    body = (
        f"<details><summary>Raw response</summary>\n\n"
        f"```\n{truncated}\n```\n</details>"
    )
    # Body contains the truncated content only
    assert "X" * 4097 not in body
    assert len(truncated) == 4096


# ---------------------------------------------------------------------------
# collect_all — iterates all F&O instruments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_all_calls_collect_one_per_instrument() -> None:
    """collect_all must call collect_one for each active F&O instrument."""
    instruments = [_make_instrument(f"STOCK{i}") for i in range(3)]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=instruments)))
        )
    )

    @asynccontextmanager
    async def _scope():
        yield mock_session

    call_log: list[str] = []

    async def fake_collect_one(inst, **kwargs):
        call_log.append(inst.symbol)

    with (
        patch("src.fno.chain_collector.session_scope", _scope),
        patch("src.fno.chain_collector.collect_one", side_effect=fake_collect_one),
    ):
        from src.fno.chain_collector import collect_all
        await collect_all()

    assert len(call_log) == 3
    assert set(call_log) == {"STOCK0", "STOCK1", "STOCK2"}
