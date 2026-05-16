"""Smoke test for the resume-check short-circuit in run_v10_backfill_one_candidate.

The function MUST exit before invoking Claude when a v10 row already exists
for the (run_date, instrument, batch_uuid) tuple. This is the per-candidate
resume semantics the backfill plan calls for (§3.2): re-running the backfill
script must not double-bill the LLM.

The test mocks the SQLAlchemy session + the Anthropic call so we can verify
the short-circuit without a real DB or API key.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.fno.thesis_synthesizer import run_v10_backfill_one_candidate


def _fake_session_scope_returning(execute_results: list):
    """Build a session-scope context-manager mock whose session.execute()
    yields each entry of ``execute_results`` in order.

    Each entry should expose a ``.scalar_one_or_none()`` method. We use
    plain MagicMock here (not AsyncMock) because the result of
    ``session.execute(...)`` is awaited, but the method call ON that
    awaited result is synchronous.
    """
    session_mock = AsyncMock()
    session_mock.execute = AsyncMock(side_effect=execute_results)

    scope_mock = MagicMock()
    scope_mock.__aenter__ = AsyncMock(return_value=session_mock)
    scope_mock.__aexit__ = AsyncMock(return_value=False)

    cm = MagicMock(return_value=scope_mock)
    return cm, session_mock


@pytest.mark.asyncio
async def test_resume_short_circuits_when_row_exists() -> None:
    """When a v10 row already exists for (D, candidate, batch_uuid), the
    function returns wrote_row=False without calling Claude."""
    batch_uuid = uuid.uuid4()
    run_date = date(2026, 4, 15)
    as_of = datetime(2026, 4, 15, 3, 30, tzinfo=timezone.utc)
    candidate_id = str(uuid.uuid4())

    # First execute() call is the resume check — return a non-None scalar
    # so the function takes the short-circuit branch.
    existing_row_result = MagicMock()
    existing_row_result.scalar_one_or_none = MagicMock(return_value=12345)

    session_scope_mock, _ = _fake_session_scope_returning([existing_row_result])

    with (
        patch("src.fno.thesis_synthesizer.session_scope", session_scope_mock),
        patch("src.fno.thesis_synthesizer._call_claude_v10") as claude_mock,
    ):
        result = await run_v10_backfill_one_candidate(
            candidate_id=candidate_id,
            run_date=run_date,
            as_of=as_of,
            dryrun_run_id=batch_uuid,
            news_cutoff=as_of,
            bandit_arm_propensity=1.0 / 9.0,
            propensity_source="imputed",
        )

    # Claude must not have been called.
    assert not claude_mock.called, "v10 LLM was called despite existing row"

    # The short-circuit return shape is documented in the function:
    assert result["wrote_row"] is False
    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0


@pytest.mark.asyncio
async def test_resume_short_circuits_when_candidate_missing() -> None:
    """When the Phase-2 candidate row doesn't exist for (D, candidate),
    we also short-circuit before any LLM call."""
    batch_uuid = uuid.uuid4()
    run_date = date(2026, 4, 15)
    as_of = datetime(2026, 4, 15, 3, 30, tzinfo=timezone.utc)
    candidate_id = str(uuid.uuid4())

    # First execute = resume check returns None (no existing row).
    # Second execute = Phase-2 lookup returns None (no candidate).
    no_existing = MagicMock()
    no_existing.scalar_one_or_none = MagicMock(return_value=None)
    no_candidate = MagicMock()
    no_candidate.scalar_one_or_none = MagicMock(return_value=None)

    session_scope_mock, _ = _fake_session_scope_returning(
        [no_existing, no_candidate]
    )

    with (
        patch("src.fno.thesis_synthesizer.session_scope", session_scope_mock),
        patch("src.fno.thesis_synthesizer._call_claude_v10") as claude_mock,
    ):
        result = await run_v10_backfill_one_candidate(
            candidate_id=candidate_id,
            run_date=run_date,
            as_of=as_of,
            dryrun_run_id=batch_uuid,
            news_cutoff=as_of,
            bandit_arm_propensity=1.0 / 9.0,
            propensity_source="imputed",
        )

    assert not claude_mock.called
    assert result["wrote_row"] is False
