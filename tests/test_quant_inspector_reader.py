"""Tests for ``src.quant.inspector`` (PR 2 of the Decision Inspector).

Two layers of coverage:

  * Pure mapper / aggregator tests — no DB. Cover ``_to_*`` projections,
    ``_aggregate_tick_summary``, ``_reconstruct_tournament``,
    ``_extract_sizer_outcome``, ``_compute_feature_deltas``. These hold
    almost all the actual logic; the async readers are thin SQL + map.

  * Reader smoke tests — use the repo's ``patch_session`` fixture
    (MockAsyncSession returning pre-built rows) to verify each public
    reader (a) opens the right query, (b) maps results to the expected
    dataclass shape, and (c) handles the missing-row case.

End-to-end correctness against a real DB was already verified by the PR 1
smoke run (1027 rows / 21 trades). A separate integration test against
real Postgres would just re-prove that.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.quant.feature_store import FeatureBundle
from src.quant.inspector.reader import (
    _aggregate_tick_summary,
    _compute_feature_deltas,
    _extract_sizer_outcome,
    _reconstruct_tournament,
    _to_primitive_signal,
    _to_run_metadata,
    _to_trade_record,
    _to_universe,
    list_runs,
    load_arm_history,
    load_arm_matrix,
    load_session_skeleton,
    load_tick_state,
    load_underlying_timeline,
)
from src.quant.inspector.types import (
    BanditTournamentView,
    PrimitiveSignalView,
    SessionSkeleton,
    TickSummary,
    TradeRecord,
)
from tests.conftest import MockAsyncSession, _FakeResult


def _patch_reader_session(monkeypatch, session):
    """Patch the inspector reader's local ``session_scope`` reference.

    The reader does ``from src.db import session_scope`` so the symbol is
    bound at import time — patching ``src.db.session_scope`` is too late.
    We patch the reader's namespace directly. Returns the same session for
    test assertions on ``.added`` / ``.execute_results``.
    """
    @asynccontextmanager
    async def _scope():
        yield session
    monkeypatch.setattr(
        "src.quant.inspector.reader.session_scope", _scope
    )
    return session


# ---------------------------------------------------------------------------
# Fixtures — synthetic ORM-like rows
# ---------------------------------------------------------------------------

def _mk_run_row(**overrides):
    base = dict(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        portfolio_id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        backtest_date=date(2026, 5, 8),
        started_at=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        starting_nav=Decimal("1000000"),
        final_nav=Decimal("1010000"),
        pnl_pct=Decimal("0.01"),
        trade_count=5,
        bandit_seed=42,
        universe=[
            {"id": "11111111-1111-1111-1111-111111111111", "symbol": "RELIANCE", "name": "Reliance"},
        ],
        config_snapshot={"primitives": ["momentum", "orb"]},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _mk_signal_row(
    *,
    symbol: str = "RELIANCE",
    arm_id: str | None = None,
    primitive_name: str = "momentum",
    direction: str = "bullish",
    strength: float = 0.6,
    rejection_reason: str = "lost_bandit",
    posterior_mean: float | None = 0.012,
    bandit_selected: bool = False,
    lots_sized: int | None = None,
    primitive_trace: dict | None = None,
    bandit_trace: dict | None = None,
    sizer_trace: dict | None = None,
    virtual_time: datetime | None = None,
):
    return SimpleNamespace(
        symbol=symbol,
        arm_id=arm_id or f"{symbol}_{primitive_name}",
        primitive_name=primitive_name,
        direction=direction,
        strength=Decimal(str(strength)),
        rejection_reason=rejection_reason,
        posterior_mean=(Decimal(str(posterior_mean)) if posterior_mean is not None else None),
        bandit_selected=bandit_selected,
        lots_sized=lots_sized,
        primitive_trace=primitive_trace,
        bandit_trace=bandit_trace,
        sizer_trace=sizer_trace,
        virtual_time=virtual_time or datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
    )


def _mk_trade_row(**overrides):
    base = dict(
        id=uuid.uuid4(),
        arm_id="RELIANCE_momentum",
        primitive_name="momentum",
        underlying_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        direction="bullish",
        entry_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        exit_at=datetime(2026, 5, 8, 10, 30, tzinfo=timezone.utc),
        entry_premium_net=Decimal("100.50"),
        exit_premium_net=Decimal("110.25"),
        realized_pnl=Decimal("19.50"),
        lots=2,
        exit_reason="trailing_stop",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Pure mapper tests
# ---------------------------------------------------------------------------

def test_to_run_metadata_projects_all_fields():
    row = _mk_run_row()
    md = _to_run_metadata(row)
    assert md.run_id == row.id
    assert md.starting_nav == 1000000.0
    assert md.final_nav == 1010000.0
    assert md.pnl_pct == 0.01
    assert md.trade_count == 5
    assert md.bandit_seed == 42


def test_to_run_metadata_handles_incomplete_run():
    """A run that hasn't finished — final_nav / pnl_pct may be None."""
    row = _mk_run_row(final_nav=None, pnl_pct=None, trade_count=None, completed_at=None)
    md = _to_run_metadata(row)
    assert md.final_nav is None
    assert md.pnl_pct is None
    assert md.trade_count is None
    assert md.completed_at is None


def test_to_universe_coerces_string_uuids():
    raw = [
        {"id": "11111111-1111-1111-1111-111111111111", "symbol": "RELIANCE", "name": "R"},
        {"id": "22222222-2222-2222-2222-222222222222", "symbol": "TCS", "name": None},
    ]
    out = _to_universe(raw)
    assert len(out) == 2
    assert isinstance(out[0].instrument_id, uuid.UUID)
    assert out[0].symbol == "RELIANCE"
    assert out[1].name is None


def test_to_universe_skips_malformed_entries():
    """A garbage row must not poison the whole universe — skip it, keep others."""
    raw = [
        {"id": "not-a-uuid", "symbol": "BAD"},
        {"id": "33333333-3333-3333-3333-333333333333", "symbol": "GOOD", "name": "G"},
    ]
    out = _to_universe(raw)
    assert len(out) == 1
    assert out[0].symbol == "GOOD"


def test_to_universe_empty_or_none():
    assert _to_universe(None) == []
    assert _to_universe([]) == []


def test_to_trade_record_projects_all_fields():
    row = _mk_trade_row()
    tr = _to_trade_record(row)
    assert isinstance(tr, TradeRecord)
    assert tr.entry_premium_net == 100.50
    assert tr.exit_premium_net == 110.25
    assert tr.realized_pnl == 19.50
    assert tr.lots == 2


def test_to_trade_record_handles_open_trade():
    """An open trade — exit_at / exit_premium_net / realized_pnl are None."""
    row = _mk_trade_row(exit_at=None, exit_premium_net=None, realized_pnl=None, exit_reason=None)
    tr = _to_trade_record(row)
    assert tr.exit_at is None
    assert tr.exit_premium_net is None
    assert tr.realized_pnl is None


def test_to_primitive_signal_passes_trace_through_unchanged():
    trace = {"name": "momentum", "intermediates": {"weighted_mom": 0.0023}}
    row = _mk_signal_row(primitive_trace=trace)
    sv = _to_primitive_signal(row)
    assert isinstance(sv, PrimitiveSignalView)
    # Trace is passed through as-is — UI consumers depend on the JSONB shape
    # documented in the migration; the reader doesn't reshape it.
    assert sv.primitive_trace is trace


# ---------------------------------------------------------------------------
# _aggregate_tick_summary
# ---------------------------------------------------------------------------

def test_aggregate_tick_summary_counts_each_bucket():
    t = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    rows = [
        _mk_signal_row(rejection_reason="opened"),
        _mk_signal_row(rejection_reason="lost_bandit"),
        _mk_signal_row(rejection_reason="lost_bandit"),
        _mk_signal_row(rejection_reason="weak_signal"),
        _mk_signal_row(rejection_reason="cooloff"),
        _mk_signal_row(rejection_reason="sized_zero"),
    ]
    summary = _aggregate_tick_summary(t, rows)
    assert isinstance(summary, TickSummary)
    assert summary.virtual_time == t
    assert summary.n_signals_total == 6
    assert summary.n_signals_strong == 5  # everything except weak_signal
    assert summary.n_opened == 1
    assert summary.n_lost_bandit == 2
    assert summary.n_sized_zero == 1
    assert summary.n_cooloff == 1
    assert summary.n_kill_switch == 0


def test_aggregate_tick_summary_handles_empty_tick():
    t = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    summary = _aggregate_tick_summary(t, [])
    assert summary.n_signals_total == 0
    assert summary.n_signals_strong == 0


# ---------------------------------------------------------------------------
# _reconstruct_tournament
# ---------------------------------------------------------------------------

def test_reconstruct_tournament_aggregates_per_arm_slices():
    bt_a = {
        "algo": "lints",
        "context_vector": [0.5, 0.3, 0.5, 0.5, 0.5],
        "context_dims": ["v", "t", "p", "n", "r"],
        "this_arm": {"posterior_mean": 0.01, "score": 0.018},
        "n_competitors": 2,
    }
    bt_b = {
        "algo": "lints",
        "context_vector": [0.5, 0.3, 0.5, 0.5, 0.5],
        "context_dims": ["v", "t", "p", "n", "r"],
        "this_arm": {"posterior_mean": 0.0, "score": 0.005},
        "n_competitors": 2,
    }
    rows = [
        _mk_signal_row(arm_id="A", bandit_trace=bt_a, bandit_selected=True),
        _mk_signal_row(arm_id="B", bandit_trace=bt_b, bandit_selected=False),
    ]
    tour = _reconstruct_tournament(rows)
    assert tour is not None
    assert tour.algo == "lints"
    assert tour.n_competitors == 2
    assert set(tour.arms.keys()) == {"A", "B"}
    assert tour.selected_arm_id == "A"


def test_reconstruct_tournament_returns_none_when_no_arm_competed():
    """All rows have no bandit_trace (e.g. weak / warmup tick)."""
    rows = [
        _mk_signal_row(arm_id="A", bandit_trace=None, rejection_reason="weak_signal"),
        _mk_signal_row(arm_id="B", bandit_trace=None, rejection_reason="warmup"),
    ]
    assert _reconstruct_tournament(rows) is None


def test_reconstruct_tournament_handles_no_selected_arm():
    """Bandit declined — every arm is lost_bandit, none has bandit_selected."""
    bt = {
        "algo": "lints",
        "context_vector": [0.5] * 5,
        "context_dims": ["v"] * 5,
        "this_arm": {"sampled_mean": -0.01, "score": -0.005},
        "n_competitors": 1,
    }
    rows = [
        _mk_signal_row(arm_id="A", bandit_trace=bt, rejection_reason="lost_bandit"),
    ]
    tour = _reconstruct_tournament(rows)
    assert tour is not None
    assert tour.selected_arm_id is None


# ---------------------------------------------------------------------------
# _extract_sizer_outcome
# ---------------------------------------------------------------------------

def test_extract_sizer_outcome_finds_chosen_arm():
    sizer = {
        "final_lots": 3,
        "blocking_step": None,
        "inputs": {"posterior_mean": 0.018},
        "constants": {"kelly_fraction": 0.5},
        "cascade": [{"step": "p_sigmoid", "value": 0.55, "formula": "..."}],
    }
    rows = [
        _mk_signal_row(arm_id="LOSER", bandit_selected=False, sizer_trace=None),
        _mk_signal_row(arm_id="WINNER", bandit_selected=True, sizer_trace=sizer),
    ]
    view, chosen = _extract_sizer_outcome(rows)
    assert chosen == "WINNER"
    assert view is not None
    assert view.final_lots == 3
    assert view.blocking_step is None


def test_extract_sizer_outcome_returns_none_when_no_chosen_arm():
    rows = [
        _mk_signal_row(arm_id="A", bandit_selected=False),
        _mk_signal_row(arm_id="B", bandit_selected=False),
    ]
    view, chosen = _extract_sizer_outcome(rows)
    assert view is None
    assert chosen is None


# ---------------------------------------------------------------------------
# _compute_feature_deltas
# ---------------------------------------------------------------------------

def _bundle(ltp: float, vol: float, vix: float, regime: str = "normal") -> FeatureBundle:
    return FeatureBundle(
        underlying_id=uuid.uuid4(),
        underlying_symbol="X",
        captured_at=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        underlying_ltp=ltp,
        underlying_volume_3min=vol,
        vwap_today=ltp,
        realized_vol_3min=0.20,
        realized_vol_30min=0.18,
        atm_iv=0.22,
        atm_oi=12345.0,
        atm_bid=Decimal("50.0"),
        atm_ask=Decimal("50.5"),
        bid_volume_3min_change=100.0,
        ask_volume_3min_change=80.0,
        bb_width=0.012,
        vix_value=vix,
        vix_regime=regime,
    )


def test_compute_feature_deltas_returns_one_row_per_diffable_field():
    b1 = _bundle(ltp=100.0, vol=1000.0, vix=15.0)
    b2 = _bundle(ltp=101.5, vol=1500.0, vix=15.5)
    deltas = _compute_feature_deltas(b1, b2)
    by_name = {d.name: d for d in deltas}
    assert by_name["underlying_ltp"].delta == pytest.approx(1.5)
    assert by_name["underlying_ltp"].pct_change == pytest.approx(0.015)
    assert by_name["vix_value"].delta == pytest.approx(0.5)
    # Diffable list is closed; spot-check a couple of fields are included
    assert "atm_iv" in by_name
    assert "bb_width" in by_name


def test_compute_feature_deltas_handles_missing_bundle():
    b1 = _bundle(ltp=100.0, vol=1000.0, vix=15.0)
    deltas = _compute_feature_deltas(b1, None)
    for d in deltas:
        assert d.value_t2 is None
        assert d.delta is None
        assert d.pct_change is None


def test_compute_feature_deltas_pct_change_safe_when_t1_is_zero():
    b1 = _bundle(ltp=100.0, vol=0.0, vix=15.0)  # vol = 0
    b2 = _bundle(ltp=100.0, vol=500.0, vix=15.0)
    deltas = _compute_feature_deltas(b1, b2)
    by_name = {d.name: d for d in deltas}
    assert by_name["underlying_volume_3min"].pct_change is None  # divisor was 0


# ---------------------------------------------------------------------------
# Reader smoke tests — exercise SQL pathway with mocked session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_runs_returns_typed_metadata(monkeypatch):
    """list_runs() should call execute() once and project rows via _to_run_metadata."""
    rows = [_mk_run_row(), _mk_run_row(id=uuid.UUID("00000000-0000-0000-0000-000000000099"))]
    session = MockAsyncSession(execute_results=[_FakeResult(rows)])
    _patch_reader_session(monkeypatch, session)
    result = await list_runs(uuid.UUID("00000000-0000-0000-0000-000000000010"), limit=10)
    assert len(result) == 2
    assert result[0].run_id == rows[0].id


@pytest.mark.asyncio
async def test_load_session_skeleton_returns_none_when_run_missing(monkeypatch):
    """Returns None (not exception) for unknown run_id."""
    session = MockAsyncSession()
    async def _get(model, key):
        return None
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    result = await load_session_skeleton(uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_load_session_skeleton_uses_jsonb_universe_when_populated(monkeypatch):
    """Happy path: the persisted ``universe`` JSONB is non-empty, no fallback."""
    run = _mk_run_row()  # default universe is 1 entry
    session = MockAsyncSession(
        execute_results=[_FakeResult([]), _FakeResult([])],
    )
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    skel = await load_session_skeleton(run.id)
    assert skel is not None
    assert len(skel.universe) == 1
    assert skel.universe[0].symbol == "RELIANCE"


@pytest.mark.asyncio
async def test_load_session_skeleton_derives_universe_from_signal_log(monkeypatch):
    """Old run with empty JSONB universe but populated signal log → fallback fires.

    The reader runs an extra query (instruments join) when the JSONB is
    empty AND there's any signal-log or trade data; we mock that fourth
    execute() result to return two synthetic instrument rows.
    """
    run = _mk_run_row(universe=[])  # empty JSONB
    sig_rows = [
        _mk_signal_row(symbol="RELIANCE", arm_id="RELIANCE_momentum"),
    ]
    derived_universe_rows = [
        (uuid.UUID("11111111-1111-1111-1111-111111111111"), "TCS", "Tata"),
        (uuid.UUID("22222222-2222-2222-2222-222222222222"), "RELIANCE", "Reliance"),
    ]
    session = MockAsyncSession(execute_results=[
        _FakeResult(sig_rows),                # sig_q
        _FakeResult([]),                      # trade_q
        _FakeResult(derived_universe_rows),   # _derive_universe_from_logs
    ])
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    skel = await load_session_skeleton(run.id)
    assert skel is not None
    assert len(skel.universe) == 2
    # Derived entries carry the company name from the join
    assert {u.name for u in skel.universe} == {"Tata", "Reliance"}


@pytest.mark.asyncio
async def test_load_session_skeleton_stays_empty_when_no_logs_at_all(monkeypatch):
    """Truly stale run: empty JSONB + no sig_log + no trades → universe stays []."""
    run = _mk_run_row(universe=[])
    session = MockAsyncSession(execute_results=[
        _FakeResult([]),  # sig_q
        _FakeResult([]),  # trade_q
        # No third query — the fallback skips when both sources are empty
    ])
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    skel = await load_session_skeleton(run.id)
    assert skel is not None
    assert skel.universe == []
    assert skel.ticks == []


@pytest.mark.asyncio
async def test_load_session_skeleton_aggregates_ticks_and_trades(monkeypatch):
    run = _mk_run_row()
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc)
    sig_rows = [
        _mk_signal_row(virtual_time=t1, rejection_reason="opened"),
        _mk_signal_row(virtual_time=t1, rejection_reason="lost_bandit"),
        _mk_signal_row(virtual_time=t2, rejection_reason="weak_signal"),
    ]
    trade_rows = [_mk_trade_row()]

    session = MockAsyncSession(
        execute_results=[_FakeResult(sig_rows), _FakeResult(trade_rows)],
    )
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)

    skel = await load_session_skeleton(run.id)
    assert isinstance(skel, SessionSkeleton)
    assert skel.metadata.run_id == run.id
    assert len(skel.universe) == 1
    assert len(skel.ticks) == 2  # two distinct virtual_times
    assert skel.ticks[0].virtual_time == t1
    assert skel.ticks[0].n_opened == 1
    assert skel.ticks[1].n_signals_total == 1
    assert len(skel.trades) == 1


@pytest.mark.asyncio
async def test_load_arm_history_returns_none_when_no_rows(monkeypatch):
    """An arm that never signalled returns None — distinct from empty history."""
    run = _mk_run_row()
    session = MockAsyncSession(execute_results=[_FakeResult([])])
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    result = await load_arm_history(run.id, "UNKNOWN_ARM")
    assert result is None


@pytest.mark.asyncio
async def test_load_arm_history_pulls_per_arm_bandit_slice(monkeypatch):
    run = _mk_run_row()
    bt = {
        "algo": "lints",
        "context_vector": [0.5] * 5,
        "context_dims": ["v"] * 5,
        "this_arm": {
            "posterior_mean": 0.01,
            "sampled_mean": 0.018,
            "signal_strength": 0.6,
            "score": 0.0108,
        },
        "n_competitors": 3,
    }
    rows = [
        _mk_signal_row(
            arm_id="RELIANCE_momentum", bandit_trace=bt,
            rejection_reason="lost_bandit", bandit_selected=False,
        ),
    ]
    session = MockAsyncSession(execute_results=[_FakeResult(rows)])
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    hist = await load_arm_history(run.id, "RELIANCE_momentum")
    assert hist is not None
    assert hist.arm_id == "RELIANCE_momentum"
    assert hist.primitive_name == "momentum"
    assert hist.symbol == "RELIANCE"
    assert len(hist.ticks) == 1
    assert hist.ticks[0].sampled_mean == pytest.approx(0.018)
    assert hist.ticks[0].score == pytest.approx(0.0108)


@pytest.mark.asyncio
async def test_load_tick_state_returns_none_for_unknown_run(monkeypatch):
    session = MockAsyncSession()
    async def _get(model, key):
        return None
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    out = await load_tick_state(uuid.uuid4(), datetime.now(timezone.utc))
    assert out is None


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_load_arm_matrix_returns_empty_for_unknown_run(monkeypatch):
    session = MockAsyncSession()
    async def _get(model, key):
        return None
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    out = await load_arm_matrix(uuid.uuid4())
    assert out == []


@pytest.mark.asyncio
async def test_load_arm_matrix_groups_rows_by_arm(monkeypatch):
    """One query → list of ArmHistory, one per distinct arm_id."""
    run = _mk_run_row()
    t1 = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 8, 10, 3, tzinfo=timezone.utc)
    rows = [
        _mk_signal_row(arm_id="A", virtual_time=t1, primitive_name="momentum"),
        _mk_signal_row(arm_id="A", virtual_time=t2, primitive_name="momentum"),
        _mk_signal_row(arm_id="B", virtual_time=t1, primitive_name="orb"),
    ]
    session = MockAsyncSession(execute_results=[_FakeResult(rows)])
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    out = await load_arm_matrix(run.id)
    assert {h.arm_id for h in out} == {"A", "B"}
    a = next(h for h in out if h.arm_id == "A")
    assert len(a.ticks) == 2
    # Sorted by arm_id ascending (deterministic display order)
    assert [h.arm_id for h in out] == sorted(h.arm_id for h in out)


@pytest.mark.asyncio
async def test_load_tick_state_with_no_focus_symbol_skips_feature_recompute(monkeypatch):
    """When symbol=None, ``feature_bundle`` stays None and we still get the
    signals + tournament + sizer reconstruction."""
    run = _mk_run_row()
    t = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    bt = {
        "algo": "lints",
        "context_vector": [0.5] * 5,
        "context_dims": ["v"] * 5,
        "this_arm": {"sampled_mean": 0.01, "score": 0.012},
        "n_competitors": 1,
    }
    sizer = {
        "final_lots": 2,
        "blocking_step": None,
        "inputs": {},
        "constants": {},
        "cascade": [],
    }
    sig_rows = [
        _mk_signal_row(
            virtual_time=t, bandit_trace=bt, sizer_trace=sizer,
            bandit_selected=True, rejection_reason="opened",
        ),
    ]
    session = MockAsyncSession(execute_results=[_FakeResult(sig_rows)])
    async def _get(model, key):
        return run
    session.get = _get
    _patch_reader_session(monkeypatch, session)
    out = await load_tick_state(run.id, t, symbol=None)
    assert out is not None
    assert out.feature_bundle is None  # symbol omitted ⇒ no recompute
    assert len(out.primitive_signals) == 1
    assert out.bandit_tournament is not None
    assert out.sizer_outcome is not None
    assert out.sizer_outcome.final_lots == 2
    assert out.chosen_arm_id == sig_rows[0].arm_id
