"""Tests for DhanSource adapter — auth, rate limiting, parsing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.fno.sources.dhan_source import DhanSource, _SEG_EQUITY, _SEG_INDEX
from src.fno.sources.exceptions import AuthError, RateLimitError, SchemaError

_EXPIRY = date(2026, 4, 29)


def _dhan_response() -> dict:
    """Minimal valid Dhan v2 optionchain response."""
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
# Segment routing
# ---------------------------------------------------------------------------

def test_segment_for_index():
    src = DhanSource()
    assert src._segment_for("NIFTY") == _SEG_INDEX
    assert src._segment_for("BANKNIFTY") == _SEG_INDEX


def test_segment_for_equity():
    src = DhanSource()
    assert src._segment_for("RELIANCE") == _SEG_EQUITY
    assert src._segment_for("TCS") == _SEG_EQUITY


# ---------------------------------------------------------------------------
# Auth header validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_headers_raises_auth_error_when_token_missing(monkeypatch):
    src = DhanSource()
    monkeypatch.setattr(src._settings, "dhan_access_token", "")
    monkeypatch.setattr(src._settings, "dhan_client_id", "")
    monkeypatch.setattr(src._settings, "dhan_pin", "")
    monkeypatch.setattr(src._settings, "dhan_totp_secret", "")
    with pytest.raises(AuthError):
        await src._headers()


@pytest.mark.asyncio
async def test_headers_includes_access_token(monkeypatch):
    """Static-token fallback path — used when PIN/TOTP aren't configured."""
    src = DhanSource()
    monkeypatch.setattr(src._settings, "dhan_access_token", "mytoken")
    monkeypatch.setattr(src._settings, "dhan_client_id", "myclientid")
    monkeypatch.setattr(src._settings, "dhan_pin", "")
    monkeypatch.setattr(src._settings, "dhan_totp_secret", "")
    headers = await src._headers()
    assert headers["access-token"] == "mytoken"
    assert headers["client-id"] == "myclientid"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_response_strike_count():
    src = DhanSource()
    snap = src._parse_response(_dhan_response(), "NIFTY", _EXPIRY)
    assert len(snap.strikes) == 2


def test_parse_response_underlying_ltp():
    src = DhanSource()
    snap = src._parse_response(_dhan_response(), "NIFTY", _EXPIRY)
    assert snap.underlying_ltp == Decimal("22000.0")


def test_parse_response_greeks_from_dhan():
    """Dhan returns Greeks natively — they must be passed through unchanged."""
    src = DhanSource()
    snap = src._parse_response(_dhan_response(), "NIFTY", _EXPIRY)
    call_row = next(s for s in snap.strikes if s.option_type == "CE")
    assert call_row.iv == pytest.approx(0.185)
    assert call_row.delta == pytest.approx(0.52)
    assert call_row.gamma == pytest.approx(0.0012)
    assert call_row.theta == pytest.approx(-3.50)
    assert call_row.vega == pytest.approx(8.20)


def test_parse_response_missing_data_raises_schema_error():
    src = DhanSource()
    with pytest.raises(SchemaError):
        src._parse_response({"no_data": True}, "NIFTY", _EXPIRY)


def test_parse_response_oc_not_dict_raises_schema_error():
    src = DhanSource()
    with pytest.raises(SchemaError):
        src._parse_response({"data": {"oc": "wrong_type"}}, "NIFTY", _EXPIRY)


# ---------------------------------------------------------------------------
# Token bucket — per-underlying rate limiting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_throttle_serialises_same_symbol():
    """Two calls for the same symbol must be serialised (second call waits)."""
    import asyncio
    import time
    src = DhanSource()
    src._settings.dhan_request_interval_sec = 0.05  # 50ms for speed

    timestamps: list[float] = []

    async def record_time(symbol: str):
        await src._throttle_for(symbol)
        timestamps.append(time.monotonic())

    await asyncio.gather(record_time("NIFTY"), record_time("NIFTY"))
    assert timestamps[1] - timestamps[0] >= 0.04  # at least ~interval apart


@pytest.mark.asyncio
async def test_throttle_allows_parallel_different_symbols():
    """Two calls for different symbols can run without waiting for each other."""
    import asyncio
    import time
    src = DhanSource()
    src._settings.dhan_request_interval_sec = 0.05

    start = time.monotonic()
    await asyncio.gather(
        src._throttle_for("NIFTY"),
        src._throttle_for("RELIANCE"),
    )
    elapsed = time.monotonic() - start
    # Total elapsed should be much less than 2× interval
    assert elapsed < 0.08


# ---------------------------------------------------------------------------
# fetch() — HTTP mock path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_returns_snapshot_on_200(monkeypatch):
    src = DhanSource()
    monkeypatch.setattr(src._settings, "dhan_access_token", "tok")
    monkeypatch.setattr(src._settings, "dhan_client_id", "cid")
    monkeypatch.setattr(src._settings, "dhan_pin", "")
    monkeypatch.setattr(src._settings, "dhan_totp_secret", "")
    monkeypatch.setattr(src._settings, "dhan_request_interval_sec", 0.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=_dhan_response())

    with patch.object(src._client_instance(), "post", new_callable=AsyncMock, return_value=mock_resp):
        snap = await src.fetch("NIFTY", _EXPIRY)
    assert snap.symbol == "NIFTY"
    assert len(snap.strikes) == 2


@pytest.mark.asyncio
async def test_fetch_401_raises_auth_error(monkeypatch):
    src = DhanSource()
    monkeypatch.setattr(src._settings, "dhan_access_token", "tok")
    monkeypatch.setattr(src._settings, "dhan_client_id", "cid")
    monkeypatch.setattr(src._settings, "dhan_pin", "")
    monkeypatch.setattr(src._settings, "dhan_totp_secret", "")
    monkeypatch.setattr(src._settings, "dhan_request_interval_sec", 0.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 401

    client = src._client_instance()
    with (
        patch.object(client, "post", new_callable=AsyncMock, return_value=mock_resp),
        pytest.raises(AuthError),
    ):
        await src.fetch("NIFTY", _EXPIRY)


@pytest.mark.asyncio
async def test_fetch_429_raises_rate_limit_error(monkeypatch):
    src = DhanSource()
    monkeypatch.setattr(src._settings, "dhan_access_token", "tok")
    monkeypatch.setattr(src._settings, "dhan_client_id", "cid")
    monkeypatch.setattr(src._settings, "dhan_pin", "")
    monkeypatch.setattr(src._settings, "dhan_totp_secret", "")
    monkeypatch.setattr(src._settings, "dhan_request_interval_sec", 0.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 429

    client = src._client_instance()
    with (
        patch.object(client, "post", new_callable=AsyncMock, return_value=mock_resp),
        pytest.raises(RateLimitError),
    ):
        await src.fetch("NIFTY", _EXPIRY)
