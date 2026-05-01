"""Tests for Task 1 — dryrun_run_id schema migration.

Verifies:
  - The dryrun_run_id column is declared as nullable on all 11 model classes.
  - The column carries the correct PostgreSQL UUID type.
  - Existing model instantiation (without dryrun_run_id) still works (backward compat).
  - The migration upgrade SQL adds a partial index on every affected table.
  - The migration downgrade SQL drops both the index and the column.
"""
from __future__ import annotations

import importlib
import uuid

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


# ---------------------------------------------------------------------------
# Model classes under test
# ---------------------------------------------------------------------------

from src.models.fno_candidate import FNOCandidate
from src.models.fno_signal import FNOSignal, FNOSignalEvent
from src.models.fno_cooldown import FNOCooldown
from src.models.fno_iv import IVHistory
from src.models.fno_vix import VIXTick
from src.models.notification import Notification
from src.models.llm_audit_log import LLMAuditLog
from src.models.fno_chain import OptionsChain
from src.models.fno_chain_log import ChainCollectionLog
from src.models.source import JobLog


_MODEL_CLASSES = [
    ("fno_candidates",        FNOCandidate),
    ("fno_signals",           FNOSignal),
    ("fno_signal_events",     FNOSignalEvent),
    ("fno_cooldowns",         FNOCooldown),
    ("iv_history",            IVHistory),
    ("vix_ticks",             VIXTick),
    ("notifications",         Notification),
    ("llm_audit_log",         LLMAuditLog),
    ("options_chain",         OptionsChain),
    ("chain_collection_log",  ChainCollectionLog),
    ("job_log",               JobLog),
]


# ---------------------------------------------------------------------------
# Column presence and nullability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("table_name,model_cls", _MODEL_CLASSES)
def test_dryrun_run_id_column_exists(table_name: str, model_cls):
    """dryrun_run_id must be declared as a mapped column on the model."""
    mapper = sa_inspect(model_cls)
    col_names = {c.key for c in mapper.mapper.column_attrs}
    assert "dryrun_run_id" in col_names, (
        f"{model_cls.__name__} (table={table_name}) is missing dryrun_run_id"
    )


@pytest.mark.parametrize("table_name,model_cls", _MODEL_CLASSES)
def test_dryrun_run_id_is_nullable(table_name: str, model_cls):
    """dryrun_run_id must be nullable so live inserts need not supply it."""
    mapper = sa_inspect(model_cls)
    col = mapper.mapper.columns["dryrun_run_id"]
    assert col.nullable, (
        f"{model_cls.__name__}.dryrun_run_id must be nullable"
    )


@pytest.mark.parametrize("table_name,model_cls", _MODEL_CLASSES)
def test_dryrun_run_id_is_uuid_type(table_name: str, model_cls):
    """dryrun_run_id must use the PostgreSQL UUID dialect type."""
    mapper = sa_inspect(model_cls)
    col = mapper.mapper.columns["dryrun_run_id"]
    assert isinstance(col.type, PG_UUID), (
        f"{model_cls.__name__}.dryrun_run_id should be PG_UUID, got {type(col.type)}"
    )


# ---------------------------------------------------------------------------
# Backward-compat: instantiate models without supplying dryrun_run_id
# ---------------------------------------------------------------------------

def test_fno_candidate_no_dryrun_run_id():
    """FNOCandidate can be created without passing dryrun_run_id."""
    inst_id = uuid.uuid4()
    from datetime import date
    obj = FNOCandidate(instrument_id=inst_id, run_date=date(2026, 4, 23), phase=1)
    assert obj.dryrun_run_id is None


def test_fno_signal_no_dryrun_run_id():
    """FNOSignal can be created without passing dryrun_run_id."""
    from datetime import date
    obj = FNOSignal(
        underlying_id=uuid.uuid4(),
        strategy_type="short_straddle",
        expiry_date=date(2026, 4, 24),
        legs={},
    )
    assert obj.dryrun_run_id is None


def test_job_log_no_dryrun_run_id():
    """JobLog can be created without passing dryrun_run_id."""
    obj = JobLog(job_name="test_job", status="ok")
    assert obj.dryrun_run_id is None


def test_vix_tick_no_dryrun_run_id():
    """VIXTick can be created without passing dryrun_run_id."""
    from datetime import datetime, timezone
    obj = VIXTick(
        timestamp=datetime(2026, 4, 23, 9, 15, tzinfo=timezone.utc),
        vix_value=14.5,
        regime="neutral",
    )
    assert obj.dryrun_run_id is None


# ---------------------------------------------------------------------------
# dryrun_run_id can be set to a UUID value
# ---------------------------------------------------------------------------

def test_dryrun_run_id_can_hold_uuid():
    """Assigning a UUID to dryrun_run_id is accepted."""
    from datetime import date
    run_id = uuid.uuid4()
    obj = FNOCandidate(
        instrument_id=uuid.uuid4(),
        run_date=date(2026, 4, 23),
        phase=1,
        dryrun_run_id=run_id,
    )
    assert obj.dryrun_run_id == run_id


# ---------------------------------------------------------------------------
# Migration SQL sanity checks
# ---------------------------------------------------------------------------

_MIGRATION_MODULE = "database.migrations.versions.0006_add_dryrun_run_id"

_TABLES = [
    "fno_candidates",
    "fno_signals",
    "fno_signal_events",
    "fno_cooldowns",
    "iv_history",
    "vix_ticks",
    "notifications",
    "llm_audit_log",
    "chain_collection_log",
    "options_chain",
    "job_log",
]


@pytest.fixture(scope="module")
def migration():
    return importlib.import_module(_MIGRATION_MODULE)


def test_migration_revision(migration):
    assert migration.revision == "0006_add_dryrun_run_id"
    assert migration.down_revision == "0005_chain_observability"


@pytest.mark.parametrize("table", _TABLES)
def test_upgrade_sql_adds_column(migration, table: str):
    """Upgrade SQL must contain ADD COLUMN for every target table."""
    assert f"ALTER TABLE {table} ADD COLUMN" in migration._UPGRADE_SQL, (
        f"_UPGRADE_SQL missing ADD COLUMN for {table}"
    )


@pytest.mark.parametrize("table", _TABLES)
def test_upgrade_sql_creates_partial_index(migration, table: str):
    """Upgrade SQL must create a partial index for every target table."""
    assert f"idx_{table}_dryrun_run_id" in migration._UPGRADE_SQL, (
        f"_UPGRADE_SQL missing partial index for {table}"
    )
    assert "WHERE dryrun_run_id IS NOT NULL" in migration._UPGRADE_SQL


@pytest.mark.parametrize("table", _TABLES)
def test_downgrade_sql_drops_index_and_column(migration, table: str):
    """Downgrade SQL must drop both the index and the column for every table."""
    assert f"DROP INDEX IF EXISTS idx_{table}_dryrun_run_id" in migration._DOWNGRADE_SQL, (
        f"_DOWNGRADE_SQL missing DROP INDEX for {table}"
    )
    assert f"ALTER TABLE {table} DROP COLUMN IF EXISTS dryrun_run_id" in migration._DOWNGRADE_SQL, (
        f"_DOWNGRADE_SQL missing DROP COLUMN for {table}"
    )


def test_upgrade_covers_all_11_tables(migration):
    """_UPGRADE_SQL must reference exactly the 11 expected tables."""
    for table in _TABLES:
        assert table in migration._UPGRADE_SQL


def test_downgrade_covers_all_11_tables(migration):
    """_DOWNGRADE_SQL must reference exactly the 11 expected tables."""
    for table in _TABLES:
        assert table in migration._DOWNGRADE_SQL
