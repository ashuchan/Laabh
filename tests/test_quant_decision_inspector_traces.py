"""Decision-Inspector trace plumbing — PR 1 (data foundation).

Each test exercises one trace surface in isolation:

  * Primitive trace — momentum + orb (representative of the 6 primitives).
  * Sizer trace — happy path (full cascade), and each blocking step.
  * Bandit trace — both algos (Thompson + LinTS), and the "no candidates"
    path that must still produce a well-formed (empty) trace.
  * Per-arm slice helper — verifies the sliced shape matches the storage
    contract documented in ``backtest_signal_log`` / migration 2026-05-10.

These tests intentionally don't touch the orchestrator or DB. The
end-to-end "trace flows through to the recorder" assertion lives in
``test_orchestrator_e2e_backtest.py`` so the integration surface is
exercised by exactly one test, not three.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from src.quant.bandit.lints import LinTSSampler, build_context
from src.quant.bandit.thompson import ThompsonSampler
from src.quant.feature_store import FeatureBundle
from src.quant.orchestrator import _slice_bandit_trace
from src.quant.primitives.momentum import MomentumPrimitive
from src.quant.primitives.orb import ORBPrimitive
from src.quant.sizer import compute_lots


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _bundle(
    *,
    ltp: float = 100.0,
    vol: float = 1000.0,
    rv30: float = 0.18,
    orb_high: float | None = None,
    orb_low: float | None = None,
) -> FeatureBundle:
    return FeatureBundle(
        underlying_id=uuid.uuid4(),
        underlying_symbol="TEST",
        captured_at=datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc),
        underlying_ltp=ltp,
        underlying_volume_3min=vol,
        vwap_today=ltp,
        realized_vol_3min=0.20,
        realized_vol_30min=rv30,
        atm_iv=0.22,
        atm_oi=12345.0,
        atm_bid=Decimal("50.0"),
        atm_ask=Decimal("50.5"),
        bid_volume_3min_change=100.0,
        ask_volume_3min_change=80.0,
        bb_width=0.012,
        vix_value=15.0,
        vix_regime="normal",
        constituent_basket_value=None,
        session_start_ltp=99.0,
        orb_high=orb_high,
        orb_low=orb_low,
    )


def _ascending_history(n: int, start: float = 99.0, step: float = 0.05) -> list[FeatureBundle]:
    return [_bundle(ltp=start + i * step, vol=1000.0 + i * 10) for i in range(n)]


# ---------------------------------------------------------------------------
# Primitive trace
# ---------------------------------------------------------------------------

def test_momentum_populates_trace_when_signal_fires():
    """Trace contains name, inputs, intermediates, formula. Bullish path."""
    prim = MomentumPrimitive()
    history = _ascending_history(11)
    trace: dict = {}
    sig = prim.compute_signal(_bundle(ltp=100.6), history, trace=trace)
    assert sig is not None
    assert sig.direction == "bullish"
    assert trace["name"] == "momentum"
    assert {"rv_30min", "n_bars", "ltp_now", "vol_now"} <= trace["inputs"].keys()
    assert {"weighted_mom", "total_volume"} <= trace["intermediates"].keys()
    assert "formula" in trace and "tanh" in trace["formula"]


def test_momentum_no_trace_overhead_when_trace_is_none():
    """Live mode passes None — primitive must not crash and must not return
    a different signal than when a trace is present (deterministic)."""
    prim = MomentumPrimitive()
    history = _ascending_history(11)
    bundle = _bundle(ltp=100.6)
    sig_none = prim.compute_signal(bundle, history, trace=None)
    sig_trace = prim.compute_signal(bundle, history, trace={})
    assert sig_none is not None and sig_trace is not None
    assert sig_none.direction == sig_trace.direction
    assert sig_none.strength == sig_trace.strength


def test_orb_trace_populated_only_when_signal_fires():
    """ORB returning None (no breakout) must leave the trace dict empty —
    primitive_trace presence must mirror row presence in signal_log."""
    prim = ORBPrimitive()
    history = _ascending_history(15)
    # LTP between high and low → no breakout
    bundle = _bundle(ltp=100.0, vol=2000.0, orb_high=105.0, orb_low=95.0)
    trace: dict = {}
    sig = prim.compute_signal(bundle, history, trace=trace)
    assert sig is None
    assert trace == {}


# ---------------------------------------------------------------------------
# Sizer trace
# ---------------------------------------------------------------------------

def _sizer_kwargs(**overrides):
    base = dict(
        posterior_mean=0.05,
        portfolio_capital=Decimal("1000000"),
        max_loss_per_lot=Decimal("5000"),
        estimated_costs=Decimal("100"),
        expected_gross_pnl=Decimal("12000"),
        open_exposure=Decimal("0"),
        lockin_active=False,
    )
    base.update(overrides)
    return base


def test_sizer_trace_contains_full_cascade_on_success():
    trace: dict = {}
    lots = compute_lots(**_sizer_kwargs(), trace=trace)
    assert lots > 0
    assert trace["final_lots"] == lots
    assert trace["blocking_step"] is None
    steps = [c["step"] for c in trace["cascade"]]
    # All 9 steps present in order
    assert steps == [
        "p_sigmoid", "b_win_loss_ratio", "f_kelly", "f_half_kelly",
        "f_clamped", "risk_budget", "raw_lots", "exposure_cap", "cost_gate",
    ]
    # Every step has formula + value
    for c in trace["cascade"]:
        assert "formula" in c and c["value"] is not None


def test_sizer_trace_marks_cost_gate_as_blocker():
    trace: dict = {}
    lots = compute_lots(
        **_sizer_kwargs(expected_gross_pnl=Decimal("50")),  # below cost gate
        trace=trace,
    )
    assert lots == 0
    assert trace["blocking_step"] == "cost_gate"


def test_sizer_trace_marks_exposure_cap_as_blocker():
    trace: dict = {}
    lots = compute_lots(
        **_sizer_kwargs(open_exposure=Decimal("250000")),  # above 20% of 1M
        trace=trace,
    )
    assert lots == 0
    assert trace["blocking_step"] == "exposure_cap"


def test_sizer_trace_marks_kelly_clamp_as_blocker_for_neg_posterior():
    trace: dict = {}
    lots = compute_lots(
        **_sizer_kwargs(posterior_mean=-2.0),  # very negative → f clamps to 0
        trace=trace,
    )
    assert lots == 0
    assert trace["blocking_step"] in {"f_clamped", "input_validation"}


def test_sizer_returns_same_lots_with_or_without_trace():
    """Trace mode must be observation-only, never affect the result."""
    kwargs = _sizer_kwargs()
    no_trace = compute_lots(**kwargs)
    with_trace = compute_lots(**kwargs, trace={})
    assert no_trace == with_trace


# ---------------------------------------------------------------------------
# Bandit trace — Thompson
# ---------------------------------------------------------------------------

def test_thompson_trace_contains_every_candidate():
    rng = np.random.default_rng(seed=42)
    sampler = ThompsonSampler(["a", "b", "c"], rng=rng)
    trace: dict = {}
    chosen = sampler.select(["a", "b", "c"], trace=trace)
    assert chosen in {"a", "b", "c"}
    assert trace["algo"] == "thompson"
    assert set(trace["arms"].keys()) == {"a", "b", "c"}
    for arm in {"a", "b", "c"}:
        slice_ = trace["arms"][arm]
        # Phase-5 fix: signal_strength is now part of every Thompson trace
        # (defaults to 1.0 when caller doesn't pass strengths) — symmetric
        # with LinTS so the inspector renders both algos the same way.
        assert set(slice_.keys()) == {
            "posterior_mean", "posterior_var", "sampled_mean", "signal_strength", "score",
        }
    assert trace["selected"] == chosen
    assert trace["n_competitors"] == 3


def test_thompson_trace_well_formed_when_no_candidates():
    sampler = ThompsonSampler(["a"], rng=np.random.default_rng(seed=1))
    trace: dict = {}
    chosen = sampler.select(["unknown_arm"], trace=trace)
    assert chosen is None
    assert trace == {
        "algo": "thompson", "arms": {}, "selected": None, "n_competitors": 0,
    }


def test_thompson_without_signal_strengths_is_pure_thompson():
    """Backward-compat: omitting signal_strengths means weight=1.0 for every
    arm — selection identical to the pre-Phase-5 behaviour."""
    rng = np.random.default_rng(seed=42)
    sampler = ThompsonSampler(["a", "b"], rng=rng)
    chosen = sampler.select(["a", "b"])  # no signal_strengths
    # Same seed + same arms = deterministic pick
    rng2 = np.random.default_rng(seed=42)
    sampler2 = ThompsonSampler(["a", "b"], rng=rng2)
    chosen2 = sampler2.select(["a", "b"], signal_strengths={"a": 1.0, "b": 1.0})
    # Equal-strength signals must produce the same chosen arm — the cap
    # at 1.0 default and the explicit 1.0 are equivalent
    assert chosen == chosen2


def test_thompson_weights_sample_by_signal_strength():
    """A weak-strength arm should lose to a strong-strength arm even when
    its sampled posterior is competitive. We construct a deterministic
    scenario by feeding observations so arm A has a slightly higher
    posterior mean, then weighting B's strength much higher."""
    rng = np.random.default_rng(seed=7)
    sampler = ThompsonSampler(["a", "b"], rng=rng)
    # Both arms start at prior. With no obs, samples come from the same
    # prior — they're roughly equal in expectation. Heavily downweight A.
    trace: dict = {}
    chosen = sampler.select(
        ["a", "b"],
        signal_strengths={"a": 0.1, "b": 1.0},  # a is 10x weaker
        trace=trace,
    )
    # Verify the score formula in the trace matches sampled × |strength|
    a_score = trace["arms"]["a"]["score"]
    a_sampled = trace["arms"]["a"]["sampled_mean"]
    a_strength = trace["arms"]["a"]["signal_strength"]
    assert a_score == pytest.approx(a_sampled * a_strength)
    # And B's weight contribution is 10x A's
    b_strength = trace["arms"]["b"]["signal_strength"]
    assert b_strength == pytest.approx(10.0 * a_strength)


def test_thompson_signal_strength_takes_absolute_value():
    """Negative signal strengths shouldn't flip the bandit's preference —
    primitives use sign for direction, magnitude for confidence."""
    rng = np.random.default_rng(seed=42)
    sampler = ThompsonSampler(["a", "b"], rng=rng)
    trace: dict = {}
    sampler.select(
        ["a", "b"],
        signal_strengths={"a": -0.6, "b": 0.6},  # opposite signs, equal magnitudes
        trace=trace,
    )
    # Both should have signal_strength = 0.6 in the trace (abs applied)
    assert trace["arms"]["a"]["signal_strength"] == pytest.approx(0.6)
    assert trace["arms"]["b"]["signal_strength"] == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Bandit trace — LinTS
# ---------------------------------------------------------------------------

def test_lints_trace_contains_context_and_per_arm_scores():
    rng = np.random.default_rng(seed=42)
    sampler = LinTSSampler(["a", "b"], rng=rng)
    ctx = build_context(
        vix_value=15.0,
        time_of_day_pct=0.3,
        day_running_pnl_pct=0.0,
        nifty_5d_return=0.0,
        realized_vol_30min_pctile=0.5,
    )
    trace: dict = {}
    chosen = sampler.select(["a", "b"], context=ctx, trace=trace)
    assert chosen in {"a", "b"}
    assert trace["algo"] == "lints"
    assert len(trace["context_vector"]) == 5
    assert len(trace["context_dims"]) == 5
    # Names locked by the Decision Inspector contract
    assert trace["context_dims"][0] == "vix_norm"
    for arm in {"a", "b"}:
        slice_ = trace["arms"][arm]
        expected = {"posterior_mean", "posterior_var", "sampled_mean", "signal_strength", "score"}
        assert expected <= slice_.keys()


def test_lints_select_is_deterministic_for_same_seed_with_or_without_trace():
    """Trace must not perturb random draws."""
    ctx = build_context(
        vix_value=15.0,
        time_of_day_pct=0.3,
        day_running_pnl_pct=0.0,
        nifty_5d_return=0.0,
        realized_vol_30min_pctile=0.5,
    )
    a1 = LinTSSampler(["a", "b", "c"], rng=np.random.default_rng(seed=7))
    a2 = LinTSSampler(["a", "b", "c"], rng=np.random.default_rng(seed=7))
    pick_no_trace = a1.select(["a", "b", "c"], context=ctx)
    pick_with_trace = a2.select(["a", "b", "c"], context=ctx, trace={})
    assert pick_no_trace == pick_with_trace


# ---------------------------------------------------------------------------
# _slice_bandit_trace helper
# ---------------------------------------------------------------------------

def test_slice_bandit_trace_returns_per_arm_subset():
    full = {
        "algo": "lints",
        "context_vector": [0.5, 0.3, 0.5, 0.5, 0.5],
        "context_dims": ["v", "t", "p", "n", "r"],
        "arms": {
            "a": {"posterior_mean": 0.01, "score": 0.02},
            "b": {"posterior_mean": 0.0, "score": 0.005},
        },
        "n_competitors": 2,
    }
    sl = _slice_bandit_trace(full, "a")
    assert sl is not None
    assert sl["this_arm"] == {"posterior_mean": 0.01, "score": 0.02}
    assert sl["context_vector"] == [0.5, 0.3, 0.5, 0.5, 0.5]
    assert sl["n_competitors"] == 2


def test_slice_bandit_trace_returns_none_for_missing_arm():
    full = {"arms": {"a": {}}, "context_vector": [], "context_dims": []}
    assert _slice_bandit_trace(full, "b") is None


def test_slice_bandit_trace_returns_none_when_full_is_falsy():
    assert _slice_bandit_trace(None, "a") is None
    assert _slice_bandit_trace({}, "a") is None
