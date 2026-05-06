"""Tests for src/runday/checks/connectivity.py — all external calls mocked."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runday.checks.base import Severity
from src.runday.checks.connectivity import (
    AngelOneCheck,
    AnthropicCheck,
    DBConnectivityCheck,
    DhanCheck,
    EnvCheck,
    GitHubCheck,
    NSECheck,
    TelegramCheck,
)
from src.runday.config import RundaySettings


@pytest.fixture
def settings() -> RundaySettings:
    return RundaySettings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        anthropic_api_key="test-key",
        telegram_bot_token="test-bot",
        telegram_chat_id="test-chat",
        angel_one_api_key="ak",
        angel_one_client_id="ac",
        angel_one_password="pw",
        angel_one_totp_secret="JBSWY3DPEHPK3PXP",
        dhan_client_id="dc",
        dhan_access_token="dt",
        github_token="ghp_test",
        github_repo="ashuchan/Laabh",
    )


# ---------------------------------------------------------------------------
# EnvCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_env_check_pass(settings, monkeypatch):
    required = [
        "DATABASE_URL", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_ID", "ANGEL_ONE_PASSWORD",
        "ANGEL_ONE_TOTP_SECRET", "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "GITHUB_TOKEN",
    ]
    for var in required:
        monkeypatch.setenv(var, "dummy")
    check = EnvCheck(settings)
    result = await check.run()
    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_env_check_missing(settings, monkeypatch):
    for var in ["DATABASE_URL", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID", "ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_ID",
                "ANGEL_ONE_PASSWORD", "ANGEL_ONE_TOTP_SECRET",
                "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "GITHUB_TOKEN"]:
        monkeypatch.delenv(var, raising=False)

    check = EnvCheck(settings)
    result = await check.run()
    assert result.severity == Severity.FAIL
    assert "missing" in result.details


# ---------------------------------------------------------------------------
# DBConnectivityCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_connectivity_pass(settings):
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_ctx)

    with patch("src.runday.checks.connectivity.get_engine", return_value=mock_engine):
        check = DBConnectivityCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert "latency_ms" in result.details


@pytest.mark.asyncio
async def test_db_connectivity_fail(settings):
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("Connection refused")

    with patch("src.runday.checks.connectivity.get_engine", return_value=mock_engine):
        check = DBConnectivityCheck(settings)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "Connection refused" in result.message


# ---------------------------------------------------------------------------
# AnthropicCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anthropic_check_pass(settings):
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=MagicMock())

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        check = AnthropicCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert "latency_ms" in result.details


@pytest.mark.asyncio
async def test_anthropic_check_fail(settings):
    mock_client = AsyncMock()
    mock_client.messages.create.side_effect = Exception("Invalid API key")

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        check = AnthropicCheck(settings)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "Invalid API key" in result.message


@pytest.mark.asyncio
async def test_anthropic_check_no_key(settings):
    settings_no_key = RundaySettings(anthropic_api_key="")
    check = AnthropicCheck(settings_no_key)
    result = await check.run()
    assert result.severity == Severity.FAIL
    assert "not set" in result.message


# ---------------------------------------------------------------------------
# TelegramCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_check_quiet(settings):
    check = TelegramCheck(settings, quiet=True)
    result = await check.run()
    assert result.severity == Severity.OK
    assert "suppressed" in result.message


@pytest.mark.asyncio
async def test_telegram_check_no_credentials():
    s = RundaySettings(telegram_bot_token="", telegram_chat_id="")
    check = TelegramCheck(s, quiet=False)
    result = await check.run()
    assert result.severity == Severity.WARN


@pytest.mark.asyncio
async def test_telegram_check_send_ok(settings):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        check = TelegramCheck(settings, quiet=False)
        result = await check.run()

    assert result.severity == Severity.OK


# ---------------------------------------------------------------------------
# NSECheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nse_check_pass(settings):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "records": {"data": [{}] * 50, "timestamp": "27-Apr-2026 09:15:00"}
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        check = NSECheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["strike_count"] == 50


@pytest.mark.asyncio
async def test_nse_check_fail(settings):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get.side_effect = Exception("Connection timed out")

    with patch("httpx.AsyncClient", return_value=mock_client):
        check = NSECheck(settings)
        result = await check.run()

    assert result.severity == Severity.FAIL


# ---------------------------------------------------------------------------
# DhanCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dhan_check_no_credentials(settings):
    """Auth resolution lives in src.auth.dhan_token; DhanCheck just surfaces its error."""
    from src.auth.dhan_token import DhanAuthError
    # Patch where the symbol is looked up (in connectivity), not where it's defined.
    with patch(
        "src.runday.checks.connectivity.get_dhan_headers",
        AsyncMock(side_effect=DhanAuthError("Dhan auth not configured")),
    ):
        check = DhanCheck(settings)
        result = await check.run()
    assert result.severity == Severity.FAIL
    assert "not configured" in result.message


@pytest.mark.asyncio
async def test_dhan_check_pass(settings):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    fake_headers = {"access-token": "tok", "client-id": "cid", "Content-Type": "application/json"}
    with patch("httpx.AsyncClient", return_value=mock_client), patch(
        "src.runday.checks.connectivity.get_dhan_headers", AsyncMock(return_value=fake_headers)
    ):
        check = DhanCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK


# ---------------------------------------------------------------------------
# GitHubCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_github_check_no_token():
    s = RundaySettings(github_token="")
    check = GitHubCheck(s)
    result = await check.run()
    assert result.severity == Severity.FAIL


@pytest.mark.asyncio
async def test_github_check_pass(settings):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {"x-ratelimit-remaining": "4500", "x-ratelimit-limit": "5000"}
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        check = GitHubCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK
    assert result.details["rate_remaining"] == 4500


@pytest.mark.asyncio
async def test_github_check_rate_limit_low(settings):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {"x-ratelimit-remaining": "50", "x-ratelimit-limit": "5000"}
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        check = GitHubCheck(settings)
        result = await check.run()

    assert result.severity == Severity.WARN
