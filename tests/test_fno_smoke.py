"""Smoke tests — every new module must import and instantiate without network or DB.

These are the fastest guards: if any import fails or a class can't be created
with no arguments, we catch it here before slower tests run.
"""
from __future__ import annotations

import importlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest


# ---------------------------------------------------------------------------
# Module importability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_path", [
    "src.fno.sources.exceptions",
    "src.fno.sources.base",
    "src.fno.sources.nse_source",
    "src.fno.sources.dhan_source",
    "src.fno.sources",
    "src.fno.chain_collector",
    "src.fno.tier_manager",
    "src.fno.issue_filer",
    "src.models.fno_collection_tier",
    "src.models.fno_chain_log",
    "src.models.fno_chain_issue",
    "src.models.fno_source_health",
    "src.models.fno_chain",
])
def test_module_imports(module_path: str) -> None:
    mod = importlib.import_module(module_path)
    assert mod is not None


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

def test_chain_source_error_base() -> None:
    from src.fno.sources.exceptions import ChainSourceError
    err = ChainSourceError("base error")
    assert "base error" in str(err)


def test_schema_error_carries_raw_response() -> None:
    from src.fno.sources.exceptions import SchemaError
    raw = '{"bad": "data"}' * 100
    err = SchemaError("shape mismatch", raw)
    assert len(err.raw_response) <= 8192
    assert "shape mismatch" in str(err)


def test_schema_error_truncates_raw_to_8kb() -> None:
    from src.fno.sources.exceptions import SchemaError
    raw = "x" * 20000
    err = SchemaError("too long", raw)
    assert len(err.raw_response) == 8192


def test_rate_limit_error() -> None:
    from src.fno.sources.exceptions import RateLimitError
    with pytest.raises(RateLimitError, match="429"):
        raise RateLimitError("429 from NSE")


def test_auth_error() -> None:
    from src.fno.sources.exceptions import AuthError
    err = AuthError("credentials invalid")
    assert isinstance(err, Exception)


def test_source_unavailable_error() -> None:
    from src.fno.sources.exceptions import SourceUnavailableError
    err = SourceUnavailableError("network timeout")
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# BaseChainSource — data models
# ---------------------------------------------------------------------------

def test_strike_row_defaults() -> None:
    from src.fno.sources.base import StrikeRow
    row = StrikeRow(strike=Decimal("22000"), option_type="CE")
    assert row.iv is None
    assert row.delta is None
    assert row.ltp is None


def test_strike_row_full_fields() -> None:
    from src.fno.sources.base import StrikeRow
    row = StrikeRow(
        strike=Decimal("22000"),
        option_type="PE",
        ltp=Decimal("140"),
        bid=Decimal("138"),
        ask=Decimal("142"),
        bid_qty=60,
        ask_qty=80,
        volume=9000,
        oi=70000,
        iv=0.19,
        delta=-0.48,
        gamma=0.0011,
        theta=-3.20,
        vega=7.80,
    )
    assert row.option_type == "PE"
    assert row.delta == pytest.approx(-0.48)


def test_chain_snapshot_ce_pe_filtering() -> None:
    from src.fno.sources.base import ChainSnapshot, StrikeRow
    snap = ChainSnapshot(
        symbol="NIFTY",
        expiry_date=date(2026, 4, 29),
        underlying_ltp=Decimal("22000"),
        snapshot_at=datetime.now(tz=timezone.utc),
        strikes=[
            StrikeRow(strike=Decimal("22000"), option_type="CE"),
            StrikeRow(strike=Decimal("22000"), option_type="PE"),
            StrikeRow(strike=Decimal("21900"), option_type="CE"),
        ],
    )
    assert len(snap.ce_strikes()) == 2
    assert len(snap.pe_strikes()) == 1


def test_chain_snapshot_empty_strikes() -> None:
    from src.fno.sources.base import ChainSnapshot
    snap = ChainSnapshot(
        symbol="RELIANCE",
        expiry_date=date(2026, 4, 29),
        underlying_ltp=None,
        snapshot_at=datetime.now(tz=timezone.utc),
    )
    assert snap.strikes == []
    assert snap.ce_strikes() == []
    assert snap.pe_strikes() == []


# ---------------------------------------------------------------------------
# NSESource instantiation
# ---------------------------------------------------------------------------

def test_nse_source_instantiates() -> None:
    from src.fno.sources.nse_source import NSESource
    src = NSESource()
    assert src.name == "nse"
    assert src._cookies == {}


def test_nse_source_cookies_stale_when_empty() -> None:
    from src.fno.sources.nse_source import NSESource
    src = NSESource()
    assert src._cookies_stale() is True


def test_nse_source_builds_headers() -> None:
    from src.fno.sources.nse_source import NSESource
    src = NSESource()
    headers = src._build_headers()
    assert "User-Agent" in headers
    assert "Referer" in headers
    assert "Accept" in headers


# ---------------------------------------------------------------------------
# DhanSource instantiation
# ---------------------------------------------------------------------------

def test_dhan_source_instantiates() -> None:
    from src.fno.sources.dhan_source import DhanSource
    src = DhanSource()
    assert src.name == "dhan"


def test_dhan_source_segment_routing() -> None:
    from src.fno.sources.dhan_source import DhanSource, _SEG_EQUITY, _SEG_INDEX
    src = DhanSource()
    assert src._segment_for("NIFTY") == _SEG_INDEX
    assert src._segment_for("RELIANCE") == _SEG_EQUITY


# ---------------------------------------------------------------------------
# ORM models — can be instantiated without a DB session
# ---------------------------------------------------------------------------

def test_fno_collection_tier_model() -> None:
    from src.models.fno_collection_tier import FNOCollectionTier
    row = FNOCollectionTier(
        instrument_id=uuid.uuid4(),
        tier=1,
        avg_volume_5d=500_000,
    )
    assert row.tier == 1


def test_chain_collection_log_model() -> None:
    from src.models.fno_chain_log import ChainCollectionLog
    row = ChainCollectionLog(
        id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        attempted_at=datetime.now(tz=timezone.utc),
        primary_source="nse",
        status="ok",
    )
    assert row.primary_source == "nse"
    assert row.status == "ok"


def test_chain_collection_issue_model() -> None:
    from src.models.fno_chain_issue import ChainCollectionIssue
    row = ChainCollectionIssue(
        id=uuid.uuid4(),
        source="nse",
        issue_type="schema_mismatch",
        error_message="missing key",
        raw_response='{"bad": true}',
    )
    assert row.source == "nse"
    assert row.resolved_at is None


def test_source_health_model() -> None:
    from src.models.fno_source_health import SourceHealth
    row = SourceHealth(source="nse", status="healthy")
    assert row.status == "healthy"
    # SQLAlchemy column defaults are applied at INSERT time, not on Python object creation
    assert row.consecutive_errors in (0, None)


def test_options_chain_has_source_column() -> None:
    from src.models.fno_chain import OptionsChain
    # source column added in migration 0005
    assert hasattr(OptionsChain, "source")


# ---------------------------------------------------------------------------
# Config — all new fields are present and have correct types / defaults
# ---------------------------------------------------------------------------

def test_config_nse_fields() -> None:
    from src.config import Settings
    s = Settings()
    assert isinstance(s.nse_user_agent, str)
    assert len(s.nse_user_agent) > 10
    assert isinstance(s.nse_request_interval_sec, float)
    assert s.nse_request_interval_sec == pytest.approx(2.5)
    assert s.nse_cookie_refresh_interval_min == 5
    assert s.nse_max_retries == 3


def test_config_dhan_fields() -> None:
    from src.config import Settings
    s = Settings()
    assert s.dhan_client_id == ""
    assert s.dhan_access_token == ""
    assert s.dhan_request_interval_sec == pytest.approx(3.0)


def test_config_github_fields() -> None:
    from src.config import Settings
    s = Settings()
    assert s.github_repo == "ashuchan/Laabh"
    assert s.github_token == ""
    assert "bug" in s.github_issue_labels


def test_config_tier_policy_fields() -> None:
    from src.config import Settings
    s = Settings()
    assert s.fno_tier1_size == 35
    assert s.fno_tier1_cadence_min == 5
    assert s.fno_tier2_cadence_min == 15


def test_config_source_health_policy_fields() -> None:
    from src.config import Settings
    s = Settings()
    assert s.fno_source_degrade_after_schema_errors == 3
    assert s.fno_source_degrade_after_consecutive_errors == 10


def test_config_nse_primary_flag_defaults_true() -> None:
    from src.config import Settings
    s = Settings()
    assert s.fno_chain_nse_primary is True


def test_config_risk_free_rate() -> None:
    from src.config import Settings
    s = Settings()
    assert s.fno_risk_free_rate_pct == pytest.approx(6.5)


# ---------------------------------------------------------------------------
# Migration file — revision IDs and parent chain
# ---------------------------------------------------------------------------

def _load_migration_0005():
    """Load migration module — filename starts with a digit so we use importlib."""
    import importlib.util
    import pathlib
    mig_path = (
        pathlib.Path(__file__).parent.parent
        / "database/migrations/versions/0005_chain_observability.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0005", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0005_revision_id() -> None:
    mig = _load_migration_0005()
    assert mig.revision == "0005_chain_observability"
    assert mig.down_revision == "0004_fno_intelligence_module"


def test_migration_0005_has_upgrade_and_downgrade() -> None:
    mig = _load_migration_0005()
    assert callable(mig.upgrade)
    assert callable(mig.downgrade)


# ---------------------------------------------------------------------------
# Scheduler — four new F&O job functions are callable
# ---------------------------------------------------------------------------

def test_scheduler_has_new_fno_job_functions() -> None:
    """Verify the four new job coroutines exist (import scheduler with APScheduler)."""
    try:
        import src.scheduler as sched
    except ModuleNotFoundError as exc:
        pytest.skip(f"scheduler dependency missing: {exc}")
    assert callable(sched._fno_chain_collect_tier1)
    assert callable(sched._fno_chain_collect_tier2)
    assert callable(sched._fno_tier_refresh)
    assert callable(sched._fno_issue_review_loop)


def test_scheduler_no_longer_has_old_chain_refresh() -> None:
    """The old _fno_chain_refresh was replaced by tier-specific jobs."""
    try:
        import src.scheduler as sched
    except ModuleNotFoundError as exc:
        pytest.skip(f"scheduler dependency missing: {exc}")
    assert not hasattr(sched, "_fno_chain_refresh")


# ---------------------------------------------------------------------------
# API schemas — new Pydantic models validate cleanly
# ---------------------------------------------------------------------------

def test_chain_issue_response_schema() -> None:
    from src.api.schemas.fno import ChainIssueResponse
    row = ChainIssueResponse(
        id=uuid.uuid4(),
        source="nse",
        issue_type="schema_mismatch",
        error_message="missing key",
        detected_at=datetime.now(tz=timezone.utc),
    )
    assert row.source == "nse"
    assert row.resolved_at is None
    assert row.github_issue_url is None


def test_resolve_issue_response_schema() -> None:
    from src.api.schemas.fno import ResolveIssueResponse
    resp = ResolveIssueResponse(
        id=uuid.uuid4(),
        resolved=True,
        source_health_status="healthy",
    )
    assert resp.resolved is True


def test_source_health_response_schema() -> None:
    from src.api.schemas.fno import SourceHealthResponse
    resp = SourceHealthResponse(
        source="nse",
        status="healthy",
        consecutive_errors=0,
    )
    assert resp.source == "nse"
    assert resp.status == "healthy"
