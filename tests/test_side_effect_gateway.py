"""Tests for Task 3 — SideEffectGateway."""
from __future__ import annotations

import pytest

from src.services.side_effect_gateway import (
    LiveGateway,
    NoOpGateway,
    SideEffectGateway,
    get_gateway,
    set_gateway,
)


def test_noop_gateway_captures_telegram():
    gw = NoOpGateway()
    import asyncio
    asyncio.run(gw.send_telegram("hello"))
    asyncio.run(gw.send_telegram("world"))
    captures = gw.record_capture()
    assert len(captures) == 2
    assert captures[0] == {"type": "telegram", "msg": "hello"}
    assert captures[1] == {"type": "telegram", "msg": "world"}


def test_noop_gateway_captures_github_issue():
    gw = NoOpGateway()
    import asyncio
    url = asyncio.run(gw.file_github_issue("Test title", "body text", labels=["bug"]))
    assert "noop" in url or "github" in url
    captures = gw.record_capture()
    assert len(captures) == 1
    assert captures[0]["type"] == "github_issue"
    assert captures[0]["title"] == "Test title"


def test_noop_gateway_record_capture_returns_copy():
    gw = NoOpGateway()
    import asyncio
    asyncio.run(gw.send_telegram("msg"))
    c1 = gw.record_capture()
    c1.append({"extra": "item"})
    c2 = gw.record_capture()
    assert len(c2) == 1  # original not mutated


def test_set_gateway_context_scoped():
    """set_gateway() context manager isolates the gateway to the current scope."""
    gw = NoOpGateway()
    original = get_gateway()
    with set_gateway(gw):
        assert get_gateway() is gw
    assert get_gateway() is original


@pytest.mark.asyncio
async def test_set_gateway_async_context():
    """Gateway set in context is available inside the async context."""
    gw = NoOpGateway()
    with set_gateway(gw):
        await get_gateway().send_telegram("async test")
    captures = gw.record_capture()
    assert len(captures) == 1
    assert captures[0]["msg"] == "async test"


def test_live_gateway_record_capture_empty():
    """LiveGateway.record_capture() returns an empty list."""
    gw = LiveGateway()
    assert gw.record_capture() == []


def test_noop_gateway_is_side_effect_gateway():
    """NoOpGateway satisfies the SideEffectGateway Protocol."""
    gw = NoOpGateway()
    assert isinstance(gw, SideEffectGateway)
