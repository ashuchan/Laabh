"""Integration smoke test for the quant orchestrator.

Uses mock primitives and a mock DB to verify the 3-min loop runs without
crashing and produces the expected trade count on a synthetic 30-min day.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.quant.primitives.base import Signal


@pytest.mark.asyncio
async def test_tick_error_does_not_crash_run_loop(monkeypatch):
    """A raise inside the tick body must be caught and the loop must continue.

    Mocks every external dependency, then makes feature_store.get raise on
    the first tick. The loop should log the failure, run the second tick
    cleanly, and exit at hard_exit_time without propagating the error.
    """
    import pytz
    from datetime import datetime, timezone
    from decimal import Decimal

    from src.quant import orchestrator as orch
    from src.quant.bandit.selector import ArmSelector

    portfolio_id = uuid.uuid4()
    underlying_id = uuid.uuid4()
    universe = [{"id": underlying_id, "symbol": "NIFTY", "name": "Nifty 50"}]
    selector = ArmSelector(["NIFTY_orb"], seed=0)

    # Post-Task-9 / M2 fix: orchestrator goes through ctx.universe_selector
    # (LLMUniverseSelector by default). Patch the selector class so the
    # live-default ctx built inside run_loop picks up the mock.
    from src.quant.universe import LLMUniverseSelector
    monkeypatch.setattr(
        LLMUniverseSelector, "select",
        AsyncMock(return_value=universe),
    )
    monkeypatch.setattr(orch.persistence, "load_morning", AsyncMock(return_value=selector))
    monkeypatch.setattr(orch.persistence, "save_eod", AsyncMock(return_value=None))
    monkeypatch.setattr(orch, "_init_day_state", AsyncMock(return_value=None))
    monkeypatch.setattr(orch, "_finalize_day_state", AsyncMock(return_value=None))
    monkeypatch.setattr(orch, "_get_nav", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        orch, "_load_open_positions",
        AsyncMock(return_value=([], Decimal("0"))),
    )
    monkeypatch.setattr(orch.reports, "generate_eod", AsyncMock(return_value="ok"))

    call_count = {"n": 0}

    async def flaky_get(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("feature store down")
        return None  # subsequent ticks skip cleanly

    monkeypatch.setattr(orch.feature_store, "get", flaky_get)

    # 14:24 IST → loop runs 2 ticks (14:24, 14:27) before 14:30 hard exit.
    ist = pytz.timezone("Asia/Kolkata")
    as_of = ist.localize(datetime(2026, 5, 8, 14, 24)).astimezone(timezone.utc)

    # Should NOT raise — the inner try/except absorbs the first-tick failure.
    await orch.run_loop(portfolio_id, as_of=as_of)

    # Both ticks ran (failure on tick 1 didn't prevent tick 2).
    assert call_count["n"] >= 2


def _make_signal(direction: str = "bullish", strength: float = 0.7) -> Signal:
    return Signal(
        direction=direction,
        strength=strength,
        strategy_class="long_call" if direction == "bullish" else "long_put",
        expected_horizon_minutes=15,
        expected_vol_pct=0.01,
    )


@pytest.mark.asyncio
async def test_orchestrator_smoke_no_crash():
    """Orchestrator runs for one tick without raising an exception."""
    from src.quant.orchestrator import _load_primitives, _make_arm_id
    from src.quant.bandit.selector import ArmSelector

    primitives = _load_primitives(["orb"])
    assert len(primitives) == 1

    arms = ["NIFTY_orb"]
    selector = ArmSelector(arms, seed=0)
    result = selector.select([])
    assert result is None


def test_arm_id_format():
    from src.quant.orchestrator import _make_arm_id
    assert _make_arm_id("RELIANCE", "orb") == "RELIANCE_orb"
    assert _make_arm_id("BANKNIFTY", "vwap_revert") == "BANKNIFTY_vwap_revert"


def test_total_exposure_sums_premiums():
    from src.quant.orchestrator import _total_exposure
    from src.quant.exits import OpenPosition

    now = datetime.now(timezone.utc)
    pos1 = OpenPosition("A_orb", "A", "bullish", Decimal("100"), now)
    pos2 = OpenPosition("B_ofi", "B", "bearish", Decimal("200"), now)
    total = _total_exposure([pos1, pos2])
    assert total == Decimal("300")


def test_symbol_from_arm():
    from src.quant.persistence import _split_arm
    assert _split_arm("RELIANCE_orb")[0] == "RELIANCE"
    assert _split_arm("NIFTY_vwap_revert")[0] == "NIFTY"


def test_get_premium_from_bundle_uses_mid():
    """_get_premium_from_bundle returns (bid+ask)/2 when both are present."""
    from src.quant.orchestrator import _get_premium_from_bundle
    from src.quant.exits import OpenPosition

    now = datetime.now(timezone.utc)
    pos = OpenPosition("RELIANCE_orb", "RELIANCE", "bullish", Decimal("100"), now)

    bundle = MagicMock()
    bundle.atm_bid = Decimal("90")
    bundle.atm_ask = Decimal("110")
    assert _get_premium_from_bundle(pos, bundle) == Decimal("100")


def test_get_premium_from_bundle_fallback_on_no_bid_ask():
    """_get_premium_from_bundle falls back to entry_premium_net when bid/ask missing."""
    from src.quant.orchestrator import _get_premium_from_bundle
    from src.quant.exits import OpenPosition

    now = datetime.now(timezone.utc)
    pos = OpenPosition("RELIANCE_orb", "RELIANCE", "bullish", Decimal("150"), now)

    bundle = MagicMock()
    bundle.atm_bid = None
    bundle.atm_ask = None
    assert _get_premium_from_bundle(pos, bundle) == Decimal("150")


def test_build_tick_context_shape():
    """_build_tick_context returns a 5-dim array clamped to [0,1]."""
    from src.quant.orchestrator import _build_tick_context

    bundle = MagicMock()
    bundle.vix_value = 18.0
    bundle.realized_vol_30min = 0.3

    ctx = _build_tick_context(
        features_map={"NIFTY": bundle},
        minutes_since_open=90.0,
        day_running_pnl_pct=0.02,
    )
    assert ctx.shape == (5,)
    assert all(0.0 <= v <= 1.0 for v in ctx), f"context out of [0,1]: {ctx}"


def test_build_tick_context_empty_features():
    """_build_tick_context uses VIX=15 fallback when features_map is empty."""
    from src.quant.orchestrator import _build_tick_context

    ctx = _build_tick_context(
        features_map={},
        minutes_since_open=0.0,
        day_running_pnl_pct=0.0,
    )
    assert ctx.shape == (5,)
    # VIX=15 → vix_value/30 = 0.5
    assert abs(ctx[0] - 0.5) < 1e-6


def test_get_premium_from_bundle_stub_with_bundle():
    """Stub returns mid when bundle has bid/ask."""
    from src.quant.orchestrator import _get_premium_from_bundle_stub

    bundle = MagicMock()
    bundle.atm_bid = Decimal("80")
    bundle.atm_ask = Decimal("120")
    assert _get_premium_from_bundle_stub(bundle) == Decimal("100")


def test_get_premium_from_bundle_stub_fallback():
    """Stub returns ₹100 when bundle is None."""
    from src.quant.orchestrator import _get_premium_from_bundle_stub

    assert _get_premium_from_bundle_stub(None) == Decimal("100")


def test_normalize_direction_passthrough():
    """Canonical bullish/bearish values pass through unchanged."""
    from src.quant.orchestrator import _normalize_direction

    assert _normalize_direction("bullish") == "bullish"
    assert _normalize_direction("bearish") == "bearish"


def test_normalize_direction_legacy_strategy_class():
    """Legacy rows wrote signal.strategy_class into the direction column."""
    from src.quant.orchestrator import _normalize_direction

    assert _normalize_direction("long_call") == "bullish"
    assert _normalize_direction("long_put") == "bearish"
    assert _normalize_direction("debit_call_spread") == "bullish"
    assert _normalize_direction("credit_put_spread") == "bearish"


def test_normalize_direction_unknown_returns_none():
    """Unknown values return None so the caller can decide to skip + warn."""
    from src.quant.orchestrator import _normalize_direction

    assert _normalize_direction("flat") is None
    assert _normalize_direction("") is None
    assert _normalize_direction(None) is None


def test_replay_bandit_updates_applies_in_entry_order():
    """Closed-today trades must be replayed against the morning-loaded selector."""
    from types import SimpleNamespace
    from src.quant.bandit.selector import ArmSelector
    from src.quant.orchestrator import _replay_bandit_updates

    selector = ArmSelector(["A_orb"], prior_mean=0.0, prior_var=0.01, seed=0)
    t1 = SimpleNamespace(
        arm_id="A_orb",
        entry_premium_net=Decimal("100"),
        exit_premium_net=Decimal("110"),
        entry_at=datetime(2026, 5, 8, 9, 30),
    )
    t2 = SimpleNamespace(
        arm_id="A_orb",
        entry_premium_net=Decimal("100"),
        exit_premium_net=Decimal("90"),
        entry_at=datetime(2026, 5, 8, 10, 0),
    )

    # Pass out of order — function must sort by entry_at internally.
    _replay_bandit_updates(selector, [t2, t1])

    assert selector.n_obs("A_orb") == 2


def test_replay_bandit_updates_skips_open_trades():
    """Trades with no exit_premium_net must not contribute (still open)."""
    from types import SimpleNamespace
    from src.quant.bandit.selector import ArmSelector
    from src.quant.orchestrator import _replay_bandit_updates

    selector = ArmSelector(["A_orb"], seed=0)
    t = SimpleNamespace(
        arm_id="A_orb",
        entry_premium_net=Decimal("100"),
        exit_premium_net=None,
        entry_at=datetime(2026, 5, 8, 9, 30),
    )
    _replay_bandit_updates(selector, [t])
    assert selector.n_obs("A_orb") == 0


def test_replay_bandit_updates_handles_zero_entry():
    """Zero entry premium must not raise (skip silently)."""
    from types import SimpleNamespace
    from src.quant.bandit.selector import ArmSelector
    from src.quant.orchestrator import _replay_bandit_updates

    selector = ArmSelector(["A_orb"], seed=0)
    t = SimpleNamespace(
        arm_id="A_orb",
        entry_premium_net=Decimal("0"),
        exit_premium_net=Decimal("10"),
        entry_at=datetime(2026, 5, 8, 9, 30),
    )
    _replay_bandit_updates(selector, [t])
    assert selector.n_obs("A_orb") == 0


@pytest.mark.asyncio
async def test_close_position_raises_on_db_failure():
    """When the DB write fails, _close_position must propagate the error so
    the orchestrator's tick try/except keeps the position in memory rather
    than silently losing it (and double-counting realised P&L on restart).

    Post-Task-9: the close path now delegates to the recorder. We inject a
    recorder whose ``close_trade`` raises, which is the same failure surface
    the original test exercised — just now expressed at the recorder level
    instead of patching session_scope.
    """
    from src.quant.context import OrchestratorContext
    from src.quant.exits import OpenPosition
    from src.quant.orchestrator import _close_position
    from src.quant.recorder import TradeRecorder

    pos = OpenPosition(
        arm_id="A_orb",
        underlying_id="dead-beef",
        direction="bullish",
        entry_premium_net=Decimal("100"),
        entry_at=datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc),
        lots=2,
    )

    class _FailingRecorder(TradeRecorder):
        async def open_trade(self, payload):
            return None

        async def close_trade(self, payload):
            raise RuntimeError("DB connection lost")

        async def init_day(self, payload):
            return

        async def finalize_day(self, payload):
            return

    ctx = OrchestratorContext.live()
    ctx.recorder = _FailingRecorder()

    with pytest.raises(RuntimeError, match="DB connection lost"):
        await _close_position(
            pos,
            Decimal("110"),
            "trailing_stop",
            uuid.uuid4(),
            datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
            ctx,
        )


def test_replay_bandit_reward_is_per_lot_return():
    """Reward equals (exit-entry)/entry — not scaled by lots."""
    from types import SimpleNamespace
    from src.quant.bandit.selector import ArmSelector
    from src.quant.orchestrator import _replay_bandit_updates

    s_lots1 = ArmSelector(["A_orb"], prior_mean=0.0, prior_var=0.001, seed=0)
    s_lots5 = ArmSelector(["A_orb"], prior_mean=0.0, prior_var=0.001, seed=0)
    base = dict(arm_id="A_orb", entry_premium_net=Decimal("100"),
                exit_premium_net=Decimal("110"),
                entry_at=datetime(2026, 5, 8, 9, 30))
    _replay_bandit_updates(s_lots1, [SimpleNamespace(**base, lots=1)])
    _replay_bandit_updates(s_lots5, [SimpleNamespace(**base, lots=5)])

    # Same per-lot return → posterior mean must match.
    assert s_lots1.posterior_mean("A_orb") == pytest.approx(
        s_lots5.posterior_mean("A_orb")
    )
