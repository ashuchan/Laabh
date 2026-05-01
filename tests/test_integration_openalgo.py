"""Integration tests for OpenAlgo adapter."""
from __future__ import annotations

import pytest


@pytest.mark.integration
async def test_openalgo_health():
    from src.integrations.openalgo.client import health

    result = await health()
    assert result["status"] in ("ok", "degraded", "down")


@pytest.mark.integration
async def test_get_ltp():
    from src.integrations.openalgo.client import get_ltp

    price = get_ltp("RELIANCE", "NSE")
    assert isinstance(price, float)
    assert price > 0


def test_openalgo_client_missing_key(monkeypatch):
    """Client should raise when OPENALGO_API_KEY is empty and package is absent."""
    import src.integrations.openalgo.client as mod

    original = mod.OpenAlgoAPI
    mod.OpenAlgoAPI = None  # simulate missing package
    mod._client = None
    try:
        with pytest.raises(RuntimeError, match="openalgo package is not installed"):
            mod._get_client()
    finally:
        mod.OpenAlgoAPI = original
        mod._client = None
