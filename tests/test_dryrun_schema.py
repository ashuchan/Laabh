"""Tests for Task 1 — dryrun_run_id schema migration.

Verifies:
  - The dryrun_run_id column is declared as nullable on all 15 model classes.
  - The column carries the correct PostgreSQL UUID type.
  - Existing model instantiation (without dryrun_run_id) still works (backward compat).
  - source_health deliberately does NOT have the column.
  - The migration module loads and exposes the expected revision metadata.
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
from src.models.fno_ban import FNOBanList
from src.models.fno_chain_issue import ChainCollectionIssue
from src.models.content import RawContent
from src.models.fno_collection_tier import FNOCollectionTier
from src.models.fno_source_health import SourceHealth


_MODEL_CLASSES = [
    ("fno_candidates",          FNOCandidate),
    ("fno_signals",             FNOSignal),
    ("fno_signal_events",       FNOSignalEvent),
    ("fno_cooldowns",           FNOCooldown),
    ("iv_history",              IVHistory),
    ("vix_ticks",               VIXTick),
    ("notifications",           Notification),
    ("llm_audit_log",           LLMAuditLog),
    ("options_chain",           OptionsChain),
    ("chain_collection_log",    ChainCollectionLog),
    ("job_log",                 JobLog),
    # Task 1B additions:
    ("fno_ban_list",            FNOBanList),
    ("chain_collection_issues", ChainCollectionIssue),
    ("raw_content",             RawContent),
    ("fno_collection_tiers",    FNOCollectionTier),
]


# ---------------------------------------------------------------------------
# Column presence and nullability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("table_name,model_cls", _MODEL_CLASSES)
def test_dryrun_run_id_column_exists(table_name: str, model_cls):
    """dryrun_run_id must be declared as a mapped column on the model."""
    mapper = sa_inspect(model_cls)
    col_names = {c.key for c in mapper.column_attrs}
    assert "dryrun_run_id" in col_names, (
        f"{model_cls.__name__} (table={table_name}) is missing dryrun_run_id"
    )


@pytest.mark.parametrize("table_name,model_cls", _MODEL_CLASSES)
def test_dryrun_run_id_is_nullable(table_name: str, model_cls):
    """dryrun_run_id must be nullable so live inserts need not supply it."""
    mapper = sa_inspect(model_cls)
    col = mapper.columns["dryrun_run_id"]
    assert col.nullable, (
        f"{model_cls.__name__}.dryrun_run_id must be nullable"
    )


@pytest.mark.parametrize("table_name,model_cls", _MODEL_CLASSES)
def test_dryrun_run_id_is_uuid_type(table_name: str, model_cls):
    """dryrun_run_id must use the PostgreSQL UUID dialect type."""
    mapper = sa_inspect(model_cls)
    col = mapper.columns["dryrun_run_id"]
    assert isinstance(col.type, PG_UUID), (
        f"{model_cls.__name__}.dryrun_run_id should be PG_UUID, got {type(col.type)}"
    )


# ---------------------------------------------------------------------------
# source_health must NOT have the column (intentionally excluded)
# ---------------------------------------------------------------------------

def test_source_health_does_not_have_dryrun_run_id():
    """source_health is suppressed via SideEffectGateway in Task 3, not tagged.

    Replay must not touch this table at all — adding dryrun_run_id would
    imply writes are acceptable, which they are not.
    """
    mapper = sa_inspect(SourceHealth)
    col_names = {c.key for c in mapper.column_attrs}
    assert "dryrun_run_id" not in col_names, (
        "SourceHealth must not have dryrun_run_id — "
        "replay suppresses writes to source_health via SideEffectGateway (Task 3)"
    )


# ---------------------------------------------------------------------------
# Backward-compat: instantiate models without supplying dryrun_run_id
# ---------------------------------------------------------------------------

def test_fno_candidate_no_dryrun_run_id():
    """FNOCandidate can be created without passing dryrun_run_id."""
    from datetime import date
    obj = FNOCandidate(instrument_id=uuid.uuid4(), run_date=date(2026, 4, 23), phase=1)
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


def test_fno_ban_list_no_dryrun_run_id():
    """FNOBanList can be created without passing dryrun_run_id."""
    from datetime import date
    obj = FNOBanList(instrument_id=uuid.uuid4(), symbol="NIFTY", ban_date=date(2026, 4, 23))
    assert obj.dryrun_run_id is None


def test_raw_content_no_dryrun_run_id():
    """RawContent can be created without passing dryrun_run_id."""
    obj = RawContent(source_id=uuid.uuid4(), content_hash="abc123")
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
# Migration module loads with correct metadata
# ---------------------------------------------------------------------------

_MIGRATION_MODULE = "database.migrations.versions.0006_add_dryrun_run_id"


@pytest.fixture(scope="module")
def migration():
    return importlib.import_module(_MIGRATION_MODULE)


def test_migration_module_loads(migration):
    """Migration module must load and expose correct revision metadata and callables."""
    assert migration.revision == "0006_add_dryrun_run_id"
    assert migration.down_revision == "0005_chain_observability"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_migration_tables_list_has_15_entries(migration):
    """_TABLES must contain exactly 15 entries."""
    assert len(migration._TABLES) == 15


def test_migration_tables_includes_task1b_additions(migration):
    """_TABLES must include the four tables added in Task 1B."""
    for t in ("fno_ban_list", "chain_collection_issues", "raw_content", "fno_collection_tiers"):
        assert t in migration._TABLES, f"_TABLES is missing Task 1B table: {t}"


def test_migration_excludes_source_health(migration):
    """source_health must not appear in _TABLES."""
    assert "source_health" not in migration._TABLES
