"""Schema-level checks for the backtest tables introduced in migration 0014.

These tests run without a live database — they verify:

  * The migration module imports and exposes upgrade()/downgrade().
  * All four ORM models register on Base.metadata with the right table names,
    primary keys, foreign keys, and indexes.
  * Each model can be instantiated round-trip with reasonable values.

The full Postgres alembic upgrade/downgrade cycle is covered by the integration
test runbook (Task 15) — it requires a live Postgres + TimescaleDB and is not
part of the unit-test suite.
"""
from __future__ import annotations

import importlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.db import Base
from src.models import (
    BacktestRun,
    BacktestTrade,
    PriceIntraday,
    RBIRepoHistory,
)


# ---------------------------------------------------------------------------
# Migration module imports cleanly
# ---------------------------------------------------------------------------

def test_migration_0014_imports_and_exposes_upgrade_downgrade():
    mod = importlib.import_module(
        "database.migrations.versions.0014_quant_backtest_tables"
    )
    assert mod.revision == "0014_quant_backtest_tables"
    assert mod.down_revision == "0013_quant_mode_tables"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


# ---------------------------------------------------------------------------
# Metadata registration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "table_name",
    ["price_intraday", "rbi_repo_history", "backtest_runs", "backtest_trades"],
)
def test_table_registered_on_metadata(table_name):
    assert table_name in Base.metadata.tables, (
        f"{table_name} is not in Base.metadata — model not imported in src/models/__init__.py"
    )


def test_price_intraday_schema():
    t = Base.metadata.tables["price_intraday"]
    pk_cols = {c.name for c in t.primary_key.columns}
    assert pk_cols == {"instrument_id", "timestamp"}
    assert {"open", "high", "low", "close", "volume", "vwap"} <= set(t.columns.keys())
    # Index for "recent" lookup must exist.
    assert any(idx.name == "idx_price_intraday_recent" for idx in t.indexes)


def test_rbi_repo_history_schema():
    t = Base.metadata.tables["rbi_repo_history"]
    pk_cols = {c.name for c in t.primary_key.columns}
    assert pk_cols == {"date"}
    assert {"repo_rate_pct", "source", "loaded_at"} <= set(t.columns.keys())


def test_backtest_runs_schema():
    t = Base.metadata.tables["backtest_runs"]
    pk_cols = {c.name for c in t.primary_key.columns}
    assert pk_cols == {"id"}
    expected = {
        "portfolio_id",
        "backtest_date",
        "config_snapshot",
        "universe",
        "starting_nav",
        "bandit_seed",
        "git_sha",
    }
    assert expected <= set(t.columns.keys())
    # FK on portfolio_id
    fk_targets = {fk.target_fullname for fk in t.foreign_keys}
    assert any("portfolios" in tgt for tgt in fk_targets)


def test_backtest_trades_schema():
    t = Base.metadata.tables["backtest_trades"]
    pk_cols = {c.name for c in t.primary_key.columns}
    assert pk_cols == {"id"}
    expected = {
        "backtest_run_id",
        "underlying_id",
        "primitive_name",
        "arm_id",
        "direction",
        "legs",
        "entry_at",
        "entry_premium_net",
        "estimated_costs",
        "signal_strength_at_entry",
        "kelly_fraction",
        "lots",
        "chain_source",
        "underlying_source",
    }
    assert expected <= set(t.columns.keys())
    fk_targets = {fk.target_fullname for fk in t.foreign_keys}
    assert any("backtest_runs" in tgt for tgt in fk_targets)
    assert any("instruments" in tgt for tgt in fk_targets)
    # Both indexes must be present.
    idx_names = {idx.name for idx in t.indexes}
    assert {"idx_backtest_trades_run", "idx_backtest_trades_arm"} <= idx_names


# ---------------------------------------------------------------------------
# Round-trip instantiation (no DB — just object construction)
# ---------------------------------------------------------------------------

def _ts(year=2026, month=4, day=27, hour=9, minute=15) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_price_intraday_instantiation():
    row = PriceIntraday(
        instrument_id=uuid.uuid4(),
        timestamp=_ts(),
        open=Decimal("100.00"),
        high=Decimal("101.50"),
        low=Decimal("99.50"),
        close=Decimal("100.75"),
        volume=12345,
        vwap=Decimal("100.40"),
    )
    assert row.volume == 12345
    assert float(row.high) >= float(row.low)


def test_rbi_repo_history_instantiation():
    row = RBIRepoHistory(
        date=date(2025, 4, 1),
        repo_rate_pct=Decimal("6.5000"),
        source="rbi.org.in",
    )
    assert row.repo_rate_pct == Decimal("6.5000")


def test_backtest_run_instantiation():
    row = BacktestRun(
        portfolio_id=uuid.uuid4(),
        backtest_date=date(2025, 10, 15),
        config_snapshot={"kelly_fraction": 0.5},
        universe=[{"id": str(uuid.uuid4()), "symbol": "RELIANCE"}],
        starting_nav=Decimal("1000000.00"),
        bandit_seed=42,
    )
    assert row.bandit_seed == 42
    assert row.universe[0]["symbol"] == "RELIANCE"


def test_backtest_trade_instantiation():
    row = BacktestTrade(
        backtest_run_id=uuid.uuid4(),
        underlying_id=uuid.uuid4(),
        primitive_name="orb",
        arm_id="RELIANCE_orb",
        direction="bullish",
        legs={},
        entry_at=_ts(hour=9, minute=45),
        entry_premium_net=Decimal("125.00"),
        estimated_costs=Decimal("250.00"),
        signal_strength_at_entry=Decimal("0.650"),
        posterior_mean_at_entry=Decimal("0.000123"),
        sampled_mean_at_entry=Decimal("0.000456"),
        kelly_fraction=Decimal("0.5000"),
        lots=2,
        chain_source="synthesized",
        underlying_source="dhan_intraday",
    )
    assert row.primitive_name == "orb"
    assert row.lots == 2
    assert row.chain_source == "synthesized"
