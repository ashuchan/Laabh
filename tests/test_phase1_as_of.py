"""Tests for Task 2 — as_of parameter on run_phase1 / universe queries."""
from __future__ import annotations

from datetime import datetime, timezone, date
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from src.fno.universe import run_phase1


def _fake_execute_result(rows: list | None = None) -> MagicMock:
    """Build a session.execute() return value whose ``.all()`` returns
    ``rows`` synchronously.

    Required because ``run_phase1`` reads tier + rolling-OI history via
    raw ``session.execute(...)`` calls (not through patched helpers).
    With AsyncMock alone, the chained ``.all()`` returns a coroutine,
    breaking the dict-comprehension at universe.py:303 with
    "coroutine object is not iterable".
    """
    result = MagicMock()
    result.all = MagicMock(return_value=rows or [])
    return result


def _make_session_mock() -> AsyncMock:
    """Session mock whose execute() always returns an empty fake result.

    The two unpatched session.execute(...) call sites in run_phase1
    (tier lookup + rolling-OI history) both call ``.all()`` on the
    awaited result. Returning an empty list keeps the downstream
    dict-comprehensions valid without leaking test state.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_fake_execute_result([]))
    return session


@pytest.mark.asyncio
async def test_run_phase1_as_of_derives_run_date():
    """run_phase1 with as_of but no run_date derives run_date from as_of.date()."""
    as_of = datetime(2026, 4, 23, 9, 0, 0, tzinfo=timezone.utc)

    with (
        patch("src.fno.universe.session_scope") as mock_scope,
        patch("src.fno.universe.get_banned_ids", new=AsyncMock(return_value=set())),
        patch("src.fno.universe._get_fno_instruments", new=AsyncMock(return_value=[])),
    ):
        session_mock = _make_session_mock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        results = await run_phase1(as_of=as_of)

    # Empty result because no instruments, but we confirm it doesn't crash
    assert results == []


@pytest.mark.asyncio
async def test_run_phase1_as_of_passes_to_atm_chain_query():
    """run_phase1 passes as_of to _get_atm_chain_row so chain rows are filtered by time."""
    as_of = datetime(2026, 4, 23, 9, 0, 0, tzinfo=timezone.utc)
    inst_id = uuid.uuid4()
    captured_as_of: list[datetime | None] = []

    async def fake_atm_chain_row(session, instrument_id, *, as_of=None, expiry_date=None):
        # `expiry_date` was added to _get_atm_chain_row when Phase 1
        # started pinning OI measurements to the target expiry. The
        # stub mirrors the real signature so changes to the production
        # contract surface here as immediate test failures.
        captured_as_of.append(as_of)
        return (5000, 0.3)

    async def fake_avg_vol(session, instrument_id, run_date, cutoff_date=None):
        return 2500000

    with (
        patch("src.fno.universe.session_scope") as mock_scope,
        patch("src.fno.universe.get_banned_ids", new=AsyncMock(return_value=set())),
        patch("src.fno.universe._get_fno_instruments", new=AsyncMock(return_value=[(inst_id, "NIFTY")])),
        patch("src.fno.universe._get_atm_chain_row", side_effect=fake_atm_chain_row),
        patch("src.fno.universe._get_avg_volume_5d", side_effect=fake_avg_vol),
        patch("src.fno.universe._upsert_candidate", new=AsyncMock()),
    ):
        session_mock = _make_session_mock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        await run_phase1(as_of=as_of)

    assert len(captured_as_of) >= 1
    assert all(a == as_of for a in captured_as_of)


@pytest.mark.asyncio
async def test_run_phase1_live_uses_no_as_of_filter():
    """run_phase1 without as_of passes as_of=None so _get_atm_chain_row uses latest snapshot."""
    inst_id = uuid.uuid4()
    captured_as_of: list[datetime | None] = []

    async def fake_atm_chain_row(session, instrument_id, *, as_of=None, expiry_date=None):
        # `expiry_date` was added to _get_atm_chain_row when Phase 1
        # started pinning OI measurements to the target expiry. The
        # stub mirrors the real signature so changes to the production
        # contract surface here as immediate test failures.
        captured_as_of.append(as_of)
        return (5000, 0.3)

    async def fake_avg_vol(session, instrument_id, run_date, cutoff_date=None):
        return 2500000

    with (
        patch("src.fno.universe.session_scope") as mock_scope,
        patch("src.fno.universe.get_banned_ids", new=AsyncMock(return_value=set())),
        patch("src.fno.universe._get_fno_instruments", new=AsyncMock(return_value=[(inst_id, "NIFTY")])),
        patch("src.fno.universe._get_atm_chain_row", side_effect=fake_atm_chain_row),
        patch("src.fno.universe._get_avg_volume_5d", side_effect=fake_avg_vol),
        patch("src.fno.universe._upsert_candidate", new=AsyncMock()),
    ):
        session_mock = _make_session_mock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=session_mock)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)

        await run_phase1()

    assert len(captured_as_of) >= 1
    assert all(a is None for a in captured_as_of)


@pytest.mark.asyncio
async def test_run_premarket_pipeline_as_of_threaded():
    """run_premarket_pipeline passes as_of to run_phase1."""
    from src.fno.orchestrator import run_premarket_pipeline

    as_of = datetime(2026, 4, 23, 9, 0, 0, tzinfo=timezone.utc)
    captured_as_of: list[datetime | None] = []

    async def fake_run_phase1(run_date=None, *, as_of=None):
        captured_as_of.append(as_of)
        return []

    with (
        patch("src.fno.orchestrator.run_phase1", side_effect=fake_run_phase1),
        patch("src.fno.orchestrator.run_phase2", new=AsyncMock(return_value=[])),
        patch("src.fno.orchestrator.run_phase3", new=AsyncMock(return_value=[])),
        patch("src.fno.orchestrator._settings") as mock_settings,
    ):
        mock_settings.fno_module_enabled = True

        await run_premarket_pipeline(date(2026, 4, 23), as_of=as_of)

    assert len(captured_as_of) == 1
    assert captured_as_of[0] == as_of


@pytest.mark.asyncio
async def test_run_premarket_pipeline_live_no_as_of():
    """run_premarket_pipeline without as_of passes as_of=None to run_phase1 (live behavior)."""
    from src.fno.orchestrator import run_premarket_pipeline

    captured_as_of: list[datetime | None] = []

    async def fake_run_phase1(run_date=None, *, as_of=None):
        captured_as_of.append(as_of)
        return []

    with (
        patch("src.fno.orchestrator.run_phase1", side_effect=fake_run_phase1),
        patch("src.fno.orchestrator.run_phase2", new=AsyncMock(return_value=[])),
        patch("src.fno.orchestrator.run_phase3", new=AsyncMock(return_value=[])),
        patch("src.fno.orchestrator._settings") as mock_settings,
    ):
        mock_settings.fno_module_enabled = True

        await run_premarket_pipeline(date(2026, 4, 23))

    assert len(captured_as_of) == 1
    assert captured_as_of[0] is None
