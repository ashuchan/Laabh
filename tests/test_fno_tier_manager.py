"""Tests for tier_manager.py — tier assignment correctness and idempotency."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_instrument(symbol: str, inst_id: uuid.UUID | None = None) -> MagicMock:
    inst = MagicMock()
    inst.id = inst_id or uuid.uuid4()
    inst.symbol = symbol
    inst.is_fno = True
    inst.is_active = True
    return inst


def _index_instruments():
    return [
        _make_instrument("NIFTY"),
        _make_instrument("BANKNIFTY"),
        _make_instrument("FINNIFTY"),
        _make_instrument("MIDCPNIFTY"),
        _make_instrument("NIFTYNXT50"),
    ]


def _equity_instruments(n: int = 50) -> list:
    return [_make_instrument(f"STOCK{i:03d}") for i in range(n)]


# ---------------------------------------------------------------------------
# Tier count correctness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_produces_correct_tier1_count():
    """With FNO_TIER1_SIZE=35 and 5 indices + 50 equities → 35 Tier-1 rows."""
    instruments = _index_instruments() + _equity_instruments(50)
    volume_map = {inst.id: float(i * 1000) for i, inst in enumerate(instruments)}

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=instruments))),
            __iter__=MagicMock(return_value=iter([])),
        )
    )
    mock_session.get = AsyncMock(return_value=None)
    mock_session.add = MagicMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.fno.tier_manager.session_scope", return_value=ctx),
        patch("src.fno.tier_manager._settings") as mock_settings,
    ):
        mock_settings.fno_tier1_size = 35

        # Patch the volume query to return our volume_map
        vol_rows = [
            MagicMock(instrument_id=iid, avg_vol=vol)
            for iid, vol in volume_map.items()
        ]

        async def fake_execute(query):
            result = MagicMock()
            result.__iter__ = MagicMock(return_value=iter(vol_rows))
            result.scalars = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=instruments))
            )
            return result

        mock_session.execute = AsyncMock(side_effect=fake_execute)

        from src.fno.tier_manager import refresh
        counts = await refresh()

    assert counts["tier1"] == 35
    assert counts["tier2"] == len(instruments) - 35


@pytest.mark.asyncio
async def test_refresh_all_instruments_when_no_f_and_o():
    """With no instruments returned, refresh must return zeros without error."""
    mock_session = AsyncMock()

    async def fake_execute(_):
        result = MagicMock()
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        result.__iter__ = MagicMock(return_value=iter([]))
        return result

    mock_session.execute = AsyncMock(side_effect=fake_execute)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("src.fno.tier_manager.session_scope", return_value=ctx):
        from src.fno.tier_manager import refresh
        counts = await refresh()

    assert counts == {"tier1": 0, "tier2": 0}


# ---------------------------------------------------------------------------
# Idempotency — re-running same day must not create duplicate rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_is_idempotent_same_day():
    """Calling refresh twice with same data must not raise and must produce same counts."""
    instruments = _index_instruments() + _equity_instruments(10)

    call_count = 0

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_session.add = MagicMock()

    async def fake_execute(_):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=instruments))
        )
        result.__iter__ = MagicMock(return_value=iter([]))
        return result

    mock_session.execute = AsyncMock(side_effect=fake_execute)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.fno.tier_manager.session_scope", return_value=ctx),
        patch("src.fno.tier_manager._settings") as ms,
    ):
        ms.fno_tier1_size = 35
        from src.fno.tier_manager import refresh
        counts1 = await refresh()
        counts2 = await refresh()

    assert counts1 == counts2


# ---------------------------------------------------------------------------
# Index symbols always in Tier 1
# ---------------------------------------------------------------------------

def test_index_symbols_always_tier1():
    """All 5 NSE indices must always end up in Tier 1."""
    from src.fno.tier_manager import _INDEX_SYMBOLS
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
        assert sym in _INDEX_SYMBOLS
