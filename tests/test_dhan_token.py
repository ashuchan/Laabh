"""Tests for src.auth.dhan_token — cache, refresh, fallback, transient retry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.auth.dhan_token import (
    DhanAccessToken,
    DhanAuthError,
    DhanTokenManager,
    _TransientDhanAuthError,
    get_dhan_headers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    """Fresh manager per test — module-level singleton would leak state across tests."""
    return DhanTokenManager()


@pytest.fixture
def configured(monkeypatch):
    """Stub the main settings to a TOTP-enabled configuration."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "dhan_client_id", "1000000001")
    monkeypatch.setattr(s, "dhan_pin", "111111")
    monkeypatch.setattr(s, "dhan_totp_secret", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(s, "dhan_access_token", "")
    return s


def _mock_response(body: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=body)
    resp.text = str(body)
    return resp


class _FakeAsyncClient:
    """Async-context-manager wrapper that yields a configured mock client."""

    def __init__(self, post_mock: AsyncMock):
        self._client = MagicMock()
        self._client.post = post_mock

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *args):
        return None


# ---------------------------------------------------------------------------
# DhanAccessToken
# ---------------------------------------------------------------------------

def test_token_expired_within_buffer():
    token = DhanAccessToken(
        access_token="t", client_id="c", client_name="n",
        expiry=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert token.is_expired() is True


def test_token_not_expired_with_headroom():
    token = DhanAccessToken(
        access_token="t", client_id="c", client_name="n",
        expiry=datetime.now(timezone.utc) + timedelta(hours=20),
    )
    assert token.is_expired() is False


# ---------------------------------------------------------------------------
# DhanTokenManager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caches_token_across_calls(manager, configured):
    body = {"accessToken": "abc", "dhanClientId": "x", "dhanClientName": "Y"}
    post = AsyncMock(return_value=_mock_response(body))
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)):
        first = await manager.get_access_token()
        second = await manager.get_access_token()
    assert first is second
    post.assert_called_once()


@pytest.mark.asyncio
async def test_force_refresh_mints_new_token(manager, configured):
    body = {"accessToken": "abc", "dhanClientId": "x", "dhanClientName": "Y"}
    post = AsyncMock(return_value=_mock_response(body))
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)):
        await manager.get_access_token()
        await manager.get_access_token(force_refresh=True)
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_reset_clears_cache(manager, configured):
    body = {"accessToken": "abc", "dhanClientId": "x", "dhanClientName": "Y"}
    post = AsyncMock(return_value=_mock_response(body))
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)):
        await manager.get_access_token()
        manager.reset()
        await manager.get_access_token()
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_refreshes_when_token_near_expiry(manager, configured):
    """A token within the 10-min refresh buffer is treated as expired."""
    body = {"accessToken": "abc", "dhanClientId": "x", "dhanClientName": "Y"}
    post = AsyncMock(return_value=_mock_response(body))
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)):
        token = await manager.get_access_token()
        # Manually backdate the expiry into the buffer window.
        object.__setattr__(token, "expiry", datetime.now(timezone.utc) + timedelta(minutes=5))
        manager._token = token
        await manager.get_access_token()
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_4xx_raises_dhan_auth_error_no_retry(manager, configured):
    """Bad credentials must fail fast — tenacity should NOT retry on 4xx."""
    post = AsyncMock(return_value=_mock_response(
        {"message": "Unauthorized"}, status=401
    ))
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)):
        with pytest.raises(DhanAuthError):
            await manager.get_access_token()
    post.assert_called_once()


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(manager, configured):
    """Transient server errors must be retried."""
    success_body = {"accessToken": "abc", "dhanClientId": "x", "dhanClientName": "Y"}
    post = AsyncMock(side_effect=[
        _mock_response({"err": "boom"}, status=503),
        _mock_response(success_body, status=200),
    ])
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)), \
         patch("src.auth.dhan_token.wait_exponential", lambda **_: lambda *_a, **_k: 0):
        token = await manager.get_access_token()
    assert token.access_token == "abc"
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_missing_credentials_raises_without_http_call(manager, monkeypatch):
    """If credentials aren't configured, fail before any network I/O."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "dhan_client_id", "")
    monkeypatch.setattr(s, "dhan_pin", "")
    monkeypatch.setattr(s, "dhan_totp_secret", "")
    monkeypatch.setattr(s, "dhan_access_token", "")
    post = AsyncMock()
    with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)):
        with pytest.raises(DhanAuthError):
            await manager.get_access_token()
    post.assert_not_called()


# ---------------------------------------------------------------------------
# get_dhan_headers — fallback resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_static_token_used_when_pin_missing(monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "dhan_client_id", "cid")
    monkeypatch.setattr(s, "dhan_access_token", "static-token")
    monkeypatch.setattr(s, "dhan_pin", "")
    monkeypatch.setattr(s, "dhan_totp_secret", "")
    headers = await get_dhan_headers()
    assert headers["access-token"] == "static-token"
    assert headers["client-id"] == "cid"


@pytest.mark.asyncio
async def test_unconfigured_raises_dhan_auth_error(monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "dhan_client_id", "")
    monkeypatch.setattr(s, "dhan_access_token", "")
    monkeypatch.setattr(s, "dhan_pin", "")
    monkeypatch.setattr(s, "dhan_totp_secret", "")
    with pytest.raises(DhanAuthError):
        await get_dhan_headers()


@pytest.mark.asyncio
async def test_force_refresh_propagates_to_manager(monkeypatch):
    """get_dhan_headers(force_refresh=True) must drive a real refresh on the manager path."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "dhan_client_id", "1000000001")
    monkeypatch.setattr(s, "dhan_pin", "111111")
    monkeypatch.setattr(s, "dhan_totp_secret", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(s, "dhan_access_token", "")

    body = {"accessToken": "fresh", "dhanClientId": "1000000001", "dhanClientName": "X"}
    post = AsyncMock(return_value=_mock_response(body))
    fresh_mgr = DhanTokenManager()
    with patch("src.auth.dhan_token.manager", fresh_mgr), \
         patch("httpx.AsyncClient", return_value=_FakeAsyncClient(post)):
        await get_dhan_headers()  # initial mint
        await get_dhan_headers(force_refresh=True)  # forced re-mint
    assert post.call_count == 2
