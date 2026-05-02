"""Integration tests for migration 0006_add_dryrun_run_id.

These tests require a live PostgreSQL instance with migrations 0001–0005
already applied.  They are skipped automatically when ``POSTGRES_TEST_URL``
is not set in the environment.

Run locally:

    POSTGRES_TEST_URL=postgresql://laabh:laabh@localhost:5432/laabh_test \\
        pytest tests/integration/ -v

The database named in the URL must exist.  Migrations 0001–0005 will be
applied at the start of the test session and removed at the end via the
``base_schema`` fixture.  Migration 0006 is applied and removed within each
individual test so the tests are fully independent.

TimescaleDB tests (``test_dryrun_run_id_on_timescale``) are skipped
automatically when the TimescaleDB extension is not installed in the target
database.
"""
from __future__ import annotations

import os
from typing import Generator

import importlib

import pytest
import sqlalchemy as sa

# The _TABLES list from the migration under test.
# importlib is required because the filename starts with a digit.
_migration_mod = importlib.import_module("database.migrations.versions.0006_add_dryrun_run_id")
_TABLES = _migration_mod._TABLES  # re-exported for use in this module



# ---------------------------------------------------------------------------
# Module-level skip when Postgres is unavailable
# ---------------------------------------------------------------------------

_POSTGRES_URL = os.environ.get("POSTGRES_TEST_URL", "")

if not _POSTGRES_URL:
    pytest.skip(
        "POSTGRES_TEST_URL not set — skipping Postgres integration tests. "
        "Set the variable to run, e.g.: "
        "POSTGRES_TEST_URL=postgresql://laabh:laabh@localhost:5432/laabh_test",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sync_engine() -> Generator[sa.Engine, None, None]:
    engine = sa.create_engine(_POSTGRES_URL, isolation_level="AUTOCOMMIT")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def base_schema(sync_engine: sa.Engine) -> Generator[None, None, None]:
    """Apply migrations 0001–0005 once for the module; tear down at the end."""
    import os
    from unittest.mock import MagicMock, patch

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")

    with patch("src.config.get_settings") as mock_settings:
        s = MagicMock()
        s.sync_database_url = _POSTGRES_URL
        mock_settings.return_value = s
        command.upgrade(cfg, "0005_chain_observability")

    yield

    with patch("src.config.get_settings") as mock_settings:
        s = MagicMock()
        s.sync_database_url = _POSTGRES_URL
        mock_settings.return_value = s
        command.downgrade(cfg, "base")


@pytest.fixture
def at_revision_0005(base_schema, sync_engine: sa.Engine) -> Generator[None, None, None]:
    """Ensure 0006 is not applied before each test; clean up after."""
    from unittest.mock import MagicMock, patch
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")

    # Roll back 0006 if it was somehow left applied
    def _at_0005() -> None:
        with patch("src.config.get_settings") as mock_settings:
            s = MagicMock()
            s.sync_database_url = _POSTGRES_URL
            mock_settings.return_value = s
            command.downgrade(cfg, "0005_chain_observability")

    def _to_0006() -> None:
        with patch("src.config.get_settings") as mock_settings:
            s = MagicMock()
            s.sync_database_url = _POSTGRES_URL
            mock_settings.return_value = s
            command.upgrade(cfg, "0006_add_dryrun_run_id")

    yield (_to_0006, _at_0005)

    # Always roll back to 0005 after the test
    _at_0005()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _column_exists(conn: sa.Connection, table: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'dryrun_run_id'"
        ),
        {"t": table},
    )
    return result.fetchone() is not None


def _index_exists(conn: sa.Connection, index_name: str) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :i"),
        {"i": index_name},
    )
    return result.fetchone() is not None


def _index_is_partial(conn: sa.Connection, index_name: str) -> bool:
    """Return True if the index has a WHERE predicate (is partial)."""
    result = conn.execute(
        sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :i"),
        {"i": index_name},
    )
    row = result.fetchone()
    if row is None:
        return False
    return "WHERE" in row[0].upper()


# ---------------------------------------------------------------------------
# Test 1: upgrade then downgrade is clean
# ---------------------------------------------------------------------------

def test_upgrade_then_downgrade_is_clean(at_revision_0005, sync_engine: sa.Engine) -> None:
    """Running upgrade then downgrade leaves no trace on any of the 15 tables."""
    upgrade_fn, downgrade_fn = at_revision_0005

    # --- upgrade ---
    upgrade_fn()

    with sync_engine.connect() as conn:
        for table in _TABLES:
            assert _column_exists(conn, table), (
                f"Column dryrun_run_id missing on {table} after upgrade"
            )
            idx = f"idx_{table}_dryrun_run_id"
            assert _index_exists(conn, idx), f"Index {idx} missing after upgrade"
            assert _index_is_partial(conn, idx), (
                f"Index {idx} is not partial (missing WHERE predicate)"
            )

        # Verify source_health was NOT touched
        assert not _column_exists(conn, "source_health"), (
            "source_health must NOT have dryrun_run_id after upgrade"
        )

    # --- downgrade ---
    downgrade_fn()

    with sync_engine.connect() as conn:
        for table in _TABLES:
            assert not _column_exists(conn, table), (
                f"Column dryrun_run_id still present on {table} after downgrade"
            )
            idx = f"idx_{table}_dryrun_run_id"
            assert not _index_exists(conn, idx), (
                f"Index {idx} still present after downgrade"
            )


# ---------------------------------------------------------------------------
# Test 2: upgrade is idempotent when column already exists on one table
# ---------------------------------------------------------------------------

def test_upgrade_is_idempotent_on_partial_state(
    at_revision_0005, sync_engine: sa.Engine
) -> None:
    """Upgrade succeeds even when dryrun_run_id was already added to one table.

    The migration uses ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` which is
    idempotent.  This test documents and protects that behaviour; if the
    migration were switched to ``op.add_column`` (which lacks IF NOT EXISTS),
    this test would catch the regression.
    """
    upgrade_fn, _downgrade_fn = at_revision_0005
    first_table = _TABLES[0]

    # Pre-add the column to the first table to simulate a partial earlier run
    with sync_engine.connect() as conn:
        conn.execute(
            sa.text(f"ALTER TABLE {first_table} ADD COLUMN IF NOT EXISTS dryrun_run_id UUID")
        )

    # Full upgrade must not raise
    upgrade_fn()

    with sync_engine.connect() as conn:
        for table in _TABLES:
            assert _column_exists(conn, table), (
                f"Column dryrun_run_id missing on {table} after idempotent upgrade"
            )


# ---------------------------------------------------------------------------
# Test 3: hypertable columns on TimescaleDB
# ---------------------------------------------------------------------------

def test_dryrun_run_id_on_timescale(at_revision_0005, sync_engine: sa.Engine) -> None:
    """After upgrade, the column exists on TimescaleDB hypertable chunks.

    Skipped when TimescaleDB is not installed in the test database.
    """
    with sync_engine.connect() as conn:
        result = conn.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        )
        if result.fetchone() is None:
            pytest.skip("TimescaleDB extension not installed — skipping hypertable test")

    upgrade_fn, _downgrade_fn = at_revision_0005
    upgrade_fn()

    hypertables = ("options_chain", "vix_ticks")
    with sync_engine.connect() as conn:
        for ht in hypertables:
            # Column on the hypertable parent
            assert _column_exists(conn, ht), (
                f"dryrun_run_id missing on hypertable parent {ht}"
            )

            # Column propagated to at least one chunk (if chunks exist)
            result = conn.execute(
                sa.text(
                    "SELECT c.table_name FROM _timescaledb_catalog.chunk c "
                    "JOIN _timescaledb_catalog.hypertable h "
                    "  ON h.id = c.hypertable_id "
                    "WHERE h.table_name = :ht LIMIT 1"
                ),
                {"ht": ht},
            )
            chunk_row = result.fetchone()
            if chunk_row is not None:
                chunk_table = chunk_row[0]
                assert _column_exists(conn, chunk_table), (
                    f"dryrun_run_id not propagated to chunk {chunk_table} of {ht}"
                )
