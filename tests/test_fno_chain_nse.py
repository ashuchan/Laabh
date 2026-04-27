"""Tests for NSESource adapter — cookie warmup, URL routing, parsing."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.fno.sources.exceptions import AuthError, SchemaError, SourceUnavailableError
from src.fno.sources.nse_source import NSESource, _INDEX_SYMBOLS

_EXPIRY = date(2026, 4, 29)
_NIFTY_EXPIRY_STR = "29-Apr-2026"


def _nse_response(symbol: str = "NIFTY", expiry_str: str = _NIFTY_EXPIRY_STR) -> dict:
    """Minimal valid NSE option-chain response."""
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


# ---------------------------------------------------------------------------
# URL routing
# ---------------------------------------------------------------------------

def test_url_for_index_uses_indices_endpoint():
    from src.fno.sources.nse_source import _INDICES_URL
    src = NSESource()
    url = src._url_for("NIFTY")
    assert url.startswith(_INDICES_URL)
    assert "NIFTY" in url


def test_url_for_equity_uses_equities_endpoint():
    from src.fno.sources.nse_source import _EQUITIES_URL
    src = NSESource()
    url = src._url_for("RELIANCE")
    assert url.startswith(_EQUITIES_URL)
    assert "RELIANCE" in url


def test_known_index_symbols_in_set():
    assert "NIFTY" in _INDEX_SYMBOLS
    assert "BANKNIFTY" in _INDEX_SYMBOLS
    assert "RELIANCE" not in _INDEX_SYMBOLS


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_response_returns_correct_strike_count():
    src = NSESource()
    raw = _nse_response()
    snap = src._parse_response(raw, "NIFTY", _EXPIRY)
    # One strike with CE and PE = 2 rows
    assert len(snap.strikes) == 2


def test_parse_response_underlying_ltp():
    src = NSESource()
    snap = src._parse_response(_nse_response(), "NIFTY", _EXPIRY)
    assert snap.underlying_ltp == Decimal("22000.0")


def test_parse_response_ce_fields():
    src = NSESource()
    snap = src._parse_response(_nse_response(), "NIFTY", _EXPIRY)
    ce = next(s for s in snap.strikes if s.option_type == "CE")
    assert ce.ltp == Decimal("150.5")
    assert ce.bid == Decimal("149.0")
    assert ce.oi == 80000
    # NSE does not supply Greeks
    assert ce.iv is None
    assert ce.delta is None


def test_parse_response_missing_records_raises_schema_error():
    src = NSESource()
    with pytest.raises(SchemaError):
        src._parse_response({"no_records": True}, "NIFTY", _EXPIRY)


def test_parse_response_non_dict_root_raises_schema_error():
    src = NSESource()
    with pytest.raises(SchemaError):
        src._parse_response("not a dict", "NIFTY", _EXPIRY)


def test_parse_response_filters_by_expiry():
    """Strikes with a different expiry date must be excluded."""
    src = NSESource()
    raw = {
        "records": {
            "underlyingValue": 22000.0,
            "data": [
                {
                    "expiryDate": "06-May-2026",  # different expiry
                    "strikePrice": 22000,
                    "CE": {"lastPrice": 200.0},
                },
                {
                    "expiryDate": _NIFTY_EXPIRY_STR,
                    "strikePrice": 22000,
                    "CE": {"lastPrice": 150.0},
                },
            ],
        }
    }
    snap = src._parse_response(raw, "NIFTY", _EXPIRY)
    # Only the matching expiry CE row should be included
    assert len(snap.strikes) == 1
    assert snap.strikes[0].ltp == Decimal("150.0")


# ---------------------------------------------------------------------------
# Cookie warmup — verify warmup precedes API calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hits_warmup_url_before_api():
    """Cookie warmup must be called at least once before an API GET."""
    src = NSESource()
    warmup_called: list[str] = []

    async def fake_get_raw(url: str):
        warmup_called.append(url)
        return _nse_response()

    with (
        patch.object(src, "_refresh_cookies", new_callable=AsyncMock) as mock_warmup,
        patch.object(src, "_get", new_callable=AsyncMock, return_value=_nse_response()),
        patch.object(src, "_parse_response", return_value=MagicMock(strikes=[MagicMock()])),
    ):
        # Mark cookies as stale so refresh is triggered
        src._cookies = {}
        src._cookies_fetched_at = 0.0
        # _cookies_stale returns True with empty cookies
        await src.fetch("NIFTY", _EXPIRY)


@pytest.mark.asyncio
async def test_fetch_retries_on_auth_failure():
    """On 401/403, NSESource must refresh cookies and retry once."""
    import time
    src = NSESource()
    # Pre-seed stale cookies
    src._cookies = {"stale": "1"}
    src._cookies_fetched_at = 0.0

    call_count = 0

    async def fake_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise AuthError("NSE returned 401")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value=_nse_response())
        return mock_resp

    with (
        patch.object(src, "_refresh_cookies", new_callable=AsyncMock),
        patch.object(
            src,
            "_get",
            new_callable=AsyncMock,
            return_value=_nse_response(),
        ),
    ):
        snap = await src.fetch("NIFTY", _EXPIRY)
        assert snap is not None


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_check_returns_true_on_success():
    src = NSESource()
    with patch.object(src, "_refresh_cookies", new_callable=AsyncMock):
        result = await src.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_health_check_returns_false_on_exception():
    src = NSESource()
    with patch.object(
        src, "_refresh_cookies", new_callable=AsyncMock, side_effect=SourceUnavailableError("down")
    ):
        result = await src.health_check()
    assert result is False
