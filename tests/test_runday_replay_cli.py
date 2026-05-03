"""Tests for Task 9 — laabh-runday replay CLI command."""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.runday.cli import app

_runner = CliRunner()


def _ok_result(target_date: date):
    from src.dryrun.orchestrator import ReplayResult
    return ReplayResult(
        run_id=uuid.uuid4(),
        replay_date=target_date,
        gates_passed=["preflight.db", "preflight.bhavcopy"],
        gates_failed=[],
        captures=[{"type": "telegram", "msg": "summary"}],
        stage_counts={"chain_ticks": 10},
        success=True,
    )


@patch("src.runday.cli._replay_async", new_callable=AsyncMock)
def test_replay_requires_date(mock_replay):
    """replay without --date exits 1."""
    result = _runner.invoke(app, ["replay"])
    assert result.exit_code == 1


@patch("src.runday.cli._replay_async", new_callable=AsyncMock)
def test_replay_invalid_date_exits_1(mock_replay):
    """replay with a malformed --date exits 1."""
    result = _runner.invoke(app, ["replay", "--date", "not-a-date"])
    assert result.exit_code == 1


def test_replay_calls_async_and_exits_0(tmp_path):
    """replay with valid --date calls _replay_async and exits 0."""
    with patch("src.runday.cli._replay_async", new_callable=AsyncMock, return_value=0) as mock_async:
        result = _runner.invoke(
            app,
            ["replay", "--date", "2026-04-23", "--out", str(tmp_path)],
        )
    assert mock_async.called
    assert result.exit_code == 0


def test_replay_gate_fail_exits_20(tmp_path):
    """When _replay_async returns 20, CLI exits 20."""
    with patch("src.runday.cli._replay_async", new_callable=AsyncMock, return_value=20):
        result = _runner.invoke(
            app,
            ["replay", "--date", "2026-04-23", "--out", str(tmp_path)],
        )
    assert result.exit_code == 20


@pytest.mark.asyncio
async def test_replay_async_gate_failed_returns_20(tmp_path):
    """_replay_async returns 20 when ReplayGateFailed is raised."""
    from src.runday.cli import _replay_async
    from src.dryrun.orchestrator import ReplayGateFailed

    with patch("src.dryrun.orchestrator.replay", new=AsyncMock(side_effect=ReplayGateFailed("bhavcopy_available"))):
        code = await _replay_async(
            target_date=date(2026, 4, 23),
            mock_llm=True,
            out_dir=str(tmp_path),
            emit_json=False,
        )
    assert code == 20


@pytest.mark.asyncio
async def test_replay_async_json_flag_gate_fail_returns_20(tmp_path):
    """--json path must honour gate failures and return exit code 20.

    Bug: the original implementation always returned 0 after printing JSON,
    ignoring result.gates_failed — so automated pipelines using --json
    could never detect a gate failure.
    """
    from src.runday.cli import _replay_async
    from src.dryrun.orchestrator import ReplayResult

    target_date = date(2026, 4, 23)
    failed_result = ReplayResult(
        run_id=uuid.uuid4(),
        replay_date=target_date,
        gates_passed=[],
        gates_failed=["stage3.phase1"],
        gates_warned=[],
        captures=[],
        stage_counts={},
        success=False,
    )

    with (
        patch("src.dryrun.orchestrator.replay", new=AsyncMock(return_value=failed_result)),
        patch("src.runday.scripts.daily_report.build_report", new=AsyncMock(return_value={})),
        patch("src.runday.scripts.daily_report.format_markdown_report", return_value="# Report"),
    ):
        code = await _replay_async(
            target_date=target_date,
            mock_llm=True,
            out_dir=str(tmp_path),
            emit_json=True,
        )

    assert code == 20, "--json with gate failures must exit 20, not 0"


@pytest.mark.asyncio
async def test_replay_async_json_flag_gate_warn_returns_10(tmp_path):
    """--json path must return exit code 10 when gates are warned."""
    from src.runday.cli import _replay_async
    from src.dryrun.orchestrator import ReplayResult

    target_date = date(2026, 4, 23)
    warned_result = ReplayResult(
        run_id=uuid.uuid4(),
        replay_date=target_date,
        gates_passed=["preflight.db"],
        gates_failed=[],
        gates_warned=["stage3.phase1"],
        captures=[],
        stage_counts={},
        success=True,
    )

    with (
        patch("src.dryrun.orchestrator.replay", new=AsyncMock(return_value=warned_result)),
        patch("src.runday.scripts.daily_report.build_report", new=AsyncMock(return_value={})),
        patch("src.runday.scripts.daily_report.format_markdown_report", return_value="# Report"),
    ):
        code = await _replay_async(
            target_date=target_date,
            mock_llm=True,
            out_dir=str(tmp_path),
            emit_json=True,
        )

    assert code == 10, "--json with gate warnings must exit 10, not 0"


@pytest.mark.asyncio
async def test_replay_async_success_writes_report(tmp_path):
    """_replay_async on success writes a markdown report file."""
    from src.runday.cli import _replay_async

    target_date = date(2026, 4, 23)
    ok_result = _ok_result(target_date)

    with (
        patch("src.dryrun.orchestrator.replay", new=AsyncMock(return_value=ok_result)),
        patch("src.runday.scripts.daily_report.build_report", new=AsyncMock(return_value={})),
        patch("src.runday.scripts.daily_report.format_markdown_report", return_value="# Report"),
        patch("src.runday.cli._render_report_console"),
    ):
        code = await _replay_async(
            target_date=target_date,
            mock_llm=True,
            out_dir=str(tmp_path),
            emit_json=False,
        )

    assert code == 0
    # A markdown report file should have been written
    md_files = list(tmp_path.glob("replay-*.md"))
    assert len(md_files) == 1
    assert "2026-04-23" in md_files[0].name
