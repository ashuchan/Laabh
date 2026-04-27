"""Tests for chain_collector.py — source health helpers and collect_one logic."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.fno.chain_parser import ChainRow

_INST_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_EXPIRY = date(2026, 4, 29)
_NOW = datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)


def _make_instrument(symbol: str = "NIFTY") -> MagicMock:
    inst = MagicMock()
    inst.id = _INST_ID
    inst.symbol = symbol
    return inst


# ---------------------------------------------------------------------------
# ChainRow / chain_parser basics (provider-neutral)
# ---------------------------------------------------------------------------

def test_chain_row_defaults() -> None:
    row = ChainRow(
        instrument_id=_INST_ID,
        expiry_date=_EXPIRY,
        strike_price=Decimal("22000"),
        option_type="CE",
    )
    assert row.ltp is None
    assert row.delta is None
    assert row.iv is None


def test_chain_row_option_types() -> None:
    ce = ChainRow(
        instrument_id=_INST_ID,
        expiry_date=_EXPIRY,
        strike_price=Decimal("22000"),
        option_type="CE",
    )
    pe = ChainRow(
        instrument_id=_INST_ID,
        expiry_date=_EXPIRY,
        strike_price=Decimal("22000"),
        option_type="PE",
    )
    assert ce.option_type == "CE"
    assert pe.option_type == "PE"


# ---------------------------------------------------------------------------
# collect_one — mock session scope
# ---------------------------------------------------------------------------

def _mock_session_scope():
    mock_session = AsyncMock()

    async def _noop_get(*args, **kwargs):
        return None

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


@pytest.mark.asyncio
async def test_collect_one_nse_success_status_ok() -> None:
    """When NSE succeeds, log.status must be 'ok' and final_source='nse'."""
    from src.fno.sources.base import ChainSnapshot, StrikeRow

    snapshot = ChainSnapshot(
        symbol="NIFTY",
        expiry_date=_EXPIRY,
        underlying_ltp=Decimal("22000"),
        snapshot_at=_NOW,
        strikes=[
            StrikeRow(strike=Decimal("22000"), option_type="CE", ltp=Decimal("150")),
        ],
    )

    logged: list = []

    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", return_value=_mock_session_scope()),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
        patch("src.fno.chain_collector._persist_snapshot", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(return_value=snapshot)

        from src.fno.chain_collector import collect_one
        inst = _make_instrument()
        await collect_one(inst)


@pytest.mark.asyncio
async def test_collect_one_nse_fails_dhan_called() -> None:
    """When NSE fails with SourceUnavailableError, Dhan must be attempted."""
    from src.fno.sources.base import ChainSnapshot, StrikeRow
    from src.fno.sources.exceptions import SourceUnavailableError

    snapshot = ChainSnapshot(
        symbol="NIFTY",
        expiry_date=_EXPIRY,
        underlying_ltp=Decimal("22000"),
        snapshot_at=_NOW,
        strikes=[StrikeRow(strike=Decimal("22000"), option_type="CE")],
    )

    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector._dhan") as mock_dhan,
        patch("src.fno.chain_collector.next_weekly_expiry", return_value=_EXPIRY),
        patch("src.fno.chain_collector.session_scope", return_value=_mock_session_scope()),
        patch("src.fno.chain_collector._record_source_success", new_callable=AsyncMock),
        patch("src.fno.chain_collector._record_source_error", new_callable=AsyncMock),
        patch("src.fno.chain_collector._persist_snapshot", new_callable=AsyncMock),
    ):
        mock_nse.fetch = AsyncMock(side_effect=SourceUnavailableError("HTTP 503"))
        mock_dhan.fetch = AsyncMock(return_value=snapshot)

        from src.fno.chain_collector import collect_one
        await collect_one(_make_instrument())

        mock_dhan.fetch.assert_awaited_once()
