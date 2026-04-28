"""Tests for src/runday/checks/schema.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runday.checks.base import Severity
from src.runday.checks.schema import (
    MigrationsCurrentCheck,
    RequiredTablesCheck,
    SeedDataCheck,
    _parse_revisions,
)
from src.runday.config import RundaySettings


@pytest.fixture
def settings() -> RundaySettings:
    return RundaySettings()


# ---------------------------------------------------------------------------
# _parse_revisions helper
# ---------------------------------------------------------------------------

def test_parse_revisions_standard():
    output = "abc12345 (head)\n"
    revs = _parse_revisions(output)
    assert "abc12345" in revs


def test_parse_revisions_empty():
    assert _parse_revisions("") == set()


def test_parse_revisions_multiple():
    output = "abc12345 (head)\ndef67890 (head)\n"
    revs = _parse_revisions(output)
    assert len(revs) == 2


# ---------------------------------------------------------------------------
# MigrationsCurrentCheck
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migrations_current_pass(settings):
    head_output = "abc12345 (head)\n"
    current_output = "abc12345 (head)\n"

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = head_output
    mock_result.stderr = ""

    mock_current = MagicMock()
    mock_current.returncode = 0
    mock_current.stdout = current_output
    mock_current.stderr = ""

    with patch("subprocess.run", side_effect=[mock_result, mock_current]):
        check = MigrationsCurrentCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_migrations_unapplied(settings):
    head_output = "abc12345 (head)\n"
    current_output = "oldrev00\n"

    mock_heads = MagicMock(returncode=0, stdout=head_output, stderr="")
    mock_current = MagicMock(returncode=0, stdout=current_output, stderr="")

    with patch("subprocess.run", side_effect=[mock_heads, mock_current]):
        check = MigrationsCurrentCheck(settings)
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "Unapplied" in result.message


@pytest.mark.asyncio
async def test_migrations_alembic_not_found(settings):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        check = MigrationsCurrentCheck(settings)
        result = await check.run()

    assert result.severity == Severity.WARN
    assert "not found" in result.message


# ---------------------------------------------------------------------------
# RequiredTablesCheck
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_engine_with_tables(monkeypatch):
    """Return a factory that patches get_engine with given table names."""
    def _make(table_names: list[str]):
        mock_conn = AsyncMock()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [(t,) for t in table_names]
        mock_conn.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_ctx)
        return mock_engine
    return _make


@pytest.mark.asyncio
async def test_required_tables_all_present(settings, mock_engine_with_tables):
    tables = ["instruments", "fno_candidates", "fno_signals", "fno_signal_events",
              "fno_collection_tiers", "chain_collection_log", "chain_collection_issues",
              "source_health", "iv_history", "fno_ban_list", "vix_ticks",
              "llm_audit_log", "notifications", "job_log", "system_config", "data_sources"]
    engine = mock_engine_with_tables(tables)

    with patch("src.runday.checks.schema.get_engine", return_value=engine):
        check = RequiredTablesCheck(settings, tables=["instruments", "fno_candidates"])
        result = await check.run()

    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_required_tables_missing(settings, mock_engine_with_tables):
    engine = mock_engine_with_tables(["instruments"])

    with patch("src.runday.checks.schema.get_engine", return_value=engine):
        check = RequiredTablesCheck(settings, tables=["instruments", "fno_candidates"])
        result = await check.run()

    assert result.severity == Severity.FAIL
    assert "fno_candidates" in result.message


# ---------------------------------------------------------------------------
# SeedDataCheck
# ---------------------------------------------------------------------------

def _make_seed_engine(source_rows: list, holiday_count: int):
    """Mock engine for SeedDataCheck: two sequential execute() calls."""
    call_idx = [0]

    async def _execute(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        mock_result = MagicMock()
        if idx == 0:
            mock_result.fetchall.return_value = source_rows
        else:
            mock_result.scalar.return_value = holiday_count
        return mock_result

    mock_conn = AsyncMock()
    mock_conn.execute = _execute

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_ctx)
    return mock_engine


@pytest.mark.asyncio
async def test_seed_data_pass(settings):
    engine = _make_seed_engine([("nse",), ("dhan",), ("angel_one",)], 1)

    with patch("src.runday.checks.schema.get_engine", return_value=engine):
        check = SeedDataCheck(settings)
        result = await check.run()

    assert result.severity == Severity.OK


@pytest.mark.asyncio
async def test_seed_data_missing_sources(settings):
    engine = _make_seed_engine([("nse",)], 1)

    with patch("src.runday.checks.schema.get_engine", return_value=engine):
        check = SeedDataCheck(settings)
        result = await check.run()

    assert result.severity == Severity.WARN
    assert "dhan" in result.message or "angel_one" in result.message


@pytest.mark.asyncio
async def test_seed_data_no_holiday_calendar(settings):
    engine = _make_seed_engine([("nse",), ("dhan",), ("angel_one",)], 0)

    with patch("src.runday.checks.schema.get_engine", return_value=engine):
        check = SeedDataCheck(settings)
        result = await check.run()

    assert result.severity == Severity.WARN
    assert "holiday" in result.message.lower()
