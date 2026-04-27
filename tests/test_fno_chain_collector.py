"""Tests for F&O chain collector — _row_from_api parsing and collect() mock path."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from src.fno.chain_collector import _row_from_api
from src.fno.chain_parser import ChainRow, ChainSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INST_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SNAP_AT = datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)
_EXPIRY = date(2026, 4, 29)
_UNDERLYING = Decimal("22000")


def _api_data(**overrides) -> dict:
    """Build a minimal Angel One API option-row dict."""
    base = {
        "ltp": "150.50",
        "bidprice": "149.00",
        "askprice": "151.00",
        "bidqty": 50,
        "askqty": 75,
        "tradedVolume": 12000,
        "openInterest": 80000,
        "impliedVolatility": 0.1850,
        "delta": 0.52,
        "gamma": 0.0012,
        "theta": -3.50,
        "vega": 8.20,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _row_from_api — parser conversion tests
# ---------------------------------------------------------------------------

def test_row_from_api_basic_fields() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE", _api_data(), _UNDERLYING)
    assert isinstance(row, ChainRow)
    assert row.strike_price == Decimal("22000.0")
    assert row.option_type == "CE"
    assert row.ltp == Decimal("150.50")
    assert row.bid_price == Decimal("149.00")
    assert row.ask_price == Decimal("151.00")
    assert row.oi == 80000
    assert row.volume == 12000
    assert row.underlying_ltp == _UNDERLYING


def test_row_from_api_greeks_parsed() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE", _api_data(), _UNDERLYING)
    assert row.delta == pytest.approx(0.52)
    assert row.gamma == pytest.approx(0.0012)
    assert row.theta == pytest.approx(-3.50)
    assert row.vega == pytest.approx(8.20)
    assert row.iv == pytest.approx(0.1850)


def test_row_from_api_pe_option_type() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 21900.0, "PE", _api_data(), _UNDERLYING)
    assert row.option_type == "PE"
    assert row.strike_price == Decimal("21900.0")


def test_row_from_api_none_ltp() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE",
                        _api_data(ltp=None), _UNDERLYING)
    assert row.ltp is None


def test_row_from_api_none_bid_ask() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE",
                        _api_data(bidprice=None, askprice=None), _UNDERLYING)
    assert row.bid_price is None
    assert row.ask_price is None


def test_row_from_api_missing_all_optional_fields() -> None:
    """Row with only required fields — no error raised."""
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE", {}, _UNDERLYING)
    assert row.ltp is None
    assert row.oi is None
    assert row.iv is None
    assert row.delta is None


def test_row_from_api_invalid_decimal_value_becomes_none() -> None:
    """Non-numeric bid price should become None rather than raising."""
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE",
                        _api_data(bidprice="N/A"), _UNDERLYING)
    assert row.bid_price is None


def test_row_from_api_zero_oi() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE",
                        _api_data(openInterest=0), _UNDERLYING)
    assert row.oi == 0


def test_row_from_api_instrument_id_preserved() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE", _api_data(), _UNDERLYING)
    assert row.instrument_id == _INST_ID


def test_row_from_api_expiry_date_preserved() -> None:
    row = _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE", _api_data(), _UNDERLYING)
    assert row.expiry_date == _EXPIRY


# ---------------------------------------------------------------------------
# collect() — mock-path (snapshot_rows bypass)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_with_mock_rows_builds_snapshot() -> None:
    """collect() with snapshot_rows skips API and returns a populated ChainSnapshot."""
    from src.fno.chain_collector import collect

    instrument = MagicMock()
    instrument.id = _INST_ID
    instrument.symbol = "NIFTY"

    rows = [
        _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "CE", _api_data(), _UNDERLYING),
        _row_from_api(_INST_ID, _SNAP_AT, _EXPIRY, 22000.0, "PE", _api_data(ltp="140"), _UNDERLYING),
    ]

    with patch("src.fno.chain_collector.session_scope") as mock_scope:
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(
            scalars=lambda: MagicMock(all=lambda: [])
        ))
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await collect(instrument, snapshot_rows=rows)

    assert result is not None
    assert isinstance(result, ChainSnapshot)
    assert len(result.rows) == 2
    assert result.instrument_id == _INST_ID


@pytest.mark.asyncio
async def test_collect_empty_rows_returns_snapshot() -> None:
    from src.fno.chain_collector import collect

    instrument = MagicMock()
    instrument.id = _INST_ID
    instrument.symbol = "NIFTY"

    with patch("src.fno.chain_collector.session_scope") as mock_scope:
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(
            scalars=lambda: MagicMock(all=lambda: [])
        ))
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await collect(instrument, snapshot_rows=[])

    assert result is not None
    assert result.rows == []
