"""Tests for Task 2 — as_of parameter on chain_collector."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from src.fno.chain_collector import collect_one, collect_tier, replay_chain_source


@pytest.fixture
def fake_instrument():
    inst = MagicMock()
    inst.id = uuid.uuid4()
    inst.symbol = "NIFTY"
    inst.is_fno = True
    inst.is_active = True
    return inst


@pytest.mark.asyncio
async def test_collect_one_as_of_stamps_log(fake_instrument):
    """collect_one with as_of= stamps attempted_at with that timestamp."""
    as_of = datetime(2026, 4, 23, 9, 30, 0, tzinfo=timezone.utc)
    captured_logs = []

    async def fake_session():
        session = AsyncMock()
        session.add = lambda x: captured_logs.append(x)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    with (
        patch("src.fno.chain_collector._nse") as mock_nse,
        patch("src.fno.chain_collector.session_scope") as mock_scope,
        patch("src.fno.chain_collector.next_weekly_expiry") as mock_expiry,
        patch("src.fno.chain_collector._persist_snapshot", new=AsyncMock()),
        patch("src.fno.chain_collector._record_source_success", new=AsyncMock()),
    ):
        from src.fno.sources.base import ChainSnapshot as SourceSnapshot
        mock_snapshot = MagicMock(spec=SourceSnapshot)
        mock_snapshot.snapshot_at = as_of
        mock_snapshot.strikes = []
        mock_snapshot.expiry_date = as_of.date()
        mock_snapshot.underlying_ltp = None
        mock_nse.fetch = AsyncMock(return_value=mock_snapshot)
        mock_expiry.return_value = as_of.date()

        session_mock = AsyncMock()
        session_mock.add = lambda x: captured_logs.append(x)
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        run_id = uuid.uuid4()
        await collect_one(fake_instrument, as_of=as_of, dryrun_run_id=run_id)

    # The ChainCollectionLog added to the session should have attempted_at = as_of
    logs = [x for x in captured_logs if hasattr(x, "attempted_at")]
    assert len(logs) >= 1
    assert logs[0].attempted_at == as_of


@pytest.mark.asyncio
async def test_replay_chain_source_swaps_source(fake_instrument):
    """replay_chain_source() context manager overrides the primary source."""
    from src.fno.chain_collector import _get_active_source

    custom_source = MagicMock()
    custom_source.name = "dhan_historical"

    assert _get_active_source() is None

    with replay_chain_source(custom_source):
        assert _get_active_source() is custom_source

    # Restored after context exit
    assert _get_active_source() is None


@pytest.mark.asyncio
async def test_collect_tier_passes_as_of():
    """collect_tier forwards as_of to collect_one for each instrument."""
    as_of = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    called_with: list[dict] = []

    async def fake_collect_one(inst, *, as_of=None, dryrun_run_id=None):
        called_with.append({"as_of": as_of, "dryrun_run_id": dryrun_run_id})

    fake_inst = MagicMock()
    fake_inst.id = uuid.uuid4()
    fake_inst.symbol = "TEST"

    with (
        patch("src.fno.chain_collector.collect_one", side_effect=fake_collect_one),
        patch("src.fno.chain_collector.session_scope") as mock_scope,
    ):
        session_mock = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [fake_inst]
        session_mock.execute = AsyncMock(return_value=result_mock)
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        run_id = uuid.uuid4()
        await collect_tier(1, as_of=as_of, dryrun_run_id=run_id)

    assert len(called_with) == 1
    assert called_with[0]["as_of"] == as_of
    assert called_with[0]["dryrun_run_id"] == run_id
