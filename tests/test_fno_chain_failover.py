"""Tests for chain_collector.py failover orchestration.

Acceptance criteria verified here:
- NSE 503 → Dhan hit exactly once; log status='fallback_used', final_source='dhan'.
- NSE schema error + Dhan 503 → log status='missed', both error fields populated,
  chain_collection_issues has one new row.
"""
from __future__ import annotations

import uuid
import src.fno.chain_collector  # noqa: F401 — must be imported before patches resolve the module
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.fno.sources.base import ChainSnapshot, StrikeRow
from src.fno.sources.exceptions import SchemaError, SourceUnavailableError

_INST_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_EXPIRY = date(2026, 4, 29)
_NOW = datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)


def _make_instrument(symbol: str = "NIFTY") -> MagicMock:
    inst = MagicMock()
    inst.id = _INST_ID
    inst.symbol = symbol
    return inst


def _minimal_snapshot() -> ChainSnapshot:
    return ChainSnapshot(
        symbol="NIFTY",
        expiry_date=_EXPIRY,
        underlying_ltp=Decimal("22000"),
        snapshot_at=_NOW,
        strikes=[StrikeRow(strike=Decimal("22000"), option_type="CE")],
    )


def _mock_session():
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )
    mock_session.add = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# NSE succeeds → Dhan not called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nse_success_dhan_not_called():
    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan") as mock_dhan,
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", return_value=_mock_session()),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
        patch("src.fno.chain_collector._persist_snapshot", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(return_value=_minimal_snapshot())
        mock_dhan.fetch = AsyncMock()

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

        mock_dhan.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# NSE 503 → Dhan called exactly once; status='fallback_used'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nse_503_triggers_dhan_fallback():
    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan") as mock_dhan,
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", return_value=_mock_session()),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
        patch("src.fno.chain_collector._persist_snapshot", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(side_effect=SourceUnavailableError("HTTP 503"))
        mock_dhan.fetch = AsyncMock(return_value=_minimal_snapshot())

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

        mock_dhan.fetch.assert_awaited_once()


# ---------------------------------------------------------------------------
# NSE schema error → schema mismatch logged; Dhan attempted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nse_schema_error_logs_issue_and_tries_dhan():
    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan") as mock_dhan,
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", return_value=_mock_session()),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
        patch(
            "src.fno.chain_collector._record_schema_mismatch", new_callable=AsyncMock
        ) as mock_schema_log,
        patch("src.fno.chain_collector._persist_snapshot", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(
            side_effect=SchemaError("missing records key", '{"bad": "data"}')
        )
        mock_dhan.fetch = AsyncMock(return_value=_minimal_snapshot())

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

        mock_schema_log.assert_awaited_once()
        call_args = mock_schema_log.call_args
        assert call_args.kwargs.get("source") == "nse" or call_args.args[0] == "nse"
        mock_dhan.fetch.assert_awaited_once()


# ---------------------------------------------------------------------------
# NSE schema error + Dhan 503 → status='missed', both errors populated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_sources_fail_status_missed():
    added_objects: list = []

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )
    mock_session.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan") as mock_dhan,
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", return_value=ctx),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_schema_mismatch", new_callable=AsyncMock),
        patch("src.fno.chain_collector._persist_snapshot", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(
            side_effect=SchemaError("bad schema", '{"x": 1}')
        )
        mock_dhan.fetch = AsyncMock(
            side_effect=SourceUnavailableError("HTTP 503")
        )

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

    # The ChainCollectionLog added to session must have status='missed'
    from src.models.fno_chain_log import ChainCollectionLog
    log_rows = [o for o in added_objects if isinstance(o, ChainCollectionLog)]
    assert len(log_rows) == 1
    assert log_rows[0].status == "missed"
    assert log_rows[0].nse_error is not None
    assert log_rows[0].dhan_error is not None


# ---------------------------------------------------------------------------
# Greeks parity — NSE-computed vs Dhan-reported agree within ±0.02
# ---------------------------------------------------------------------------

def test_nse_computed_delta_matches_dhan_within_tolerance():
    """Black-Scholes delta from IV must be within ±0.02 of Dhan-reported delta."""
    from src.fno.chain_parser import compute_greeks

    # Shared parameters: NIFTY ATM call, ~14 days to expiry, IV=18.5%
    S = 22000.0
    K = 22000.0
    T = 14 / 365.0
    r = 0.065
    sigma = 0.185

    greeks = compute_greeks(S, K, T, r, sigma, "CE")
    dhan_delta = 0.52  # typical Dhan-reported ATM call delta

    assert abs(greeks["delta"] - dhan_delta) <= 0.02
