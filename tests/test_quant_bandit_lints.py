"""Tests for Linear Thompson Sampling bandit."""
from __future__ import annotations

import numpy as np
import pytest

from src.quant.bandit.lints import LinTSSampler, build_context, CONTEXT_DIM


def _rng(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def _ctx(**kw) -> np.ndarray:
    defaults = dict(
        vix_value=15.0,
        time_of_day_pct=0.5,
        day_running_pnl_pct=0.0,
        nifty_5d_return=0.0,
        realized_vol_30min_pctile=0.5,
    )
    defaults.update(kw)
    return build_context(**defaults)


def test_build_context_shape():
    ctx = _ctx()
    assert ctx.shape == (CONTEXT_DIM,)
    assert all(0.0 <= v <= 1.0 for v in ctx)


def test_select_returns_known_arm():
    arms = ["A", "B"]
    lints = LinTSSampler(arms, _rng())
    result = lints.select(arms, context=_ctx())
    assert result in arms


def test_select_none_on_empty():
    lints = LinTSSampler(["A"], _rng())
    assert lints.select([], context=_ctx()) is None


def test_snapshot_restore_round_trip():
    arms = ["A", "B"]
    lints = LinTSSampler(arms, _rng(5), prior_var=0.01)
    ctx = _ctx()
    lints.update("A", 0.05, context=ctx)
    snap = lints.snapshot()
    lints.update("A", 0.1, context=ctx)
    lints.restore(snap)
    # After restore, theta_hat matches snapshot
    assert np.allclose(lints._states["A"].theta_hat, snap["A"].theta_hat)


def test_forget_widens_posterior():
    arms = ["A"]
    lints = LinTSSampler(arms, _rng(), prior_var=0.01)
    var_before = float(np.diag(lints._states["A"].a_inv).mean())
    lints.apply_forget(0.95)
    var_after = float(np.diag(lints._states["A"].a_inv).mean())
    assert var_after == pytest.approx(var_before / 0.95, rel=1e-6)


def test_posterior_converges_towards_true_theta():
    """With known θ_a = [0.1, 0, 0, 0, 0], θ̂ should converge within 100 obs."""
    true_theta = np.array([0.1, 0.0, 0.0, 0.0, 0.0])
    arms = ["A"]
    lints = LinTSSampler(arms, _rng(42), prior_var=0.01)
    rng_data = np.random.default_rng(7)
    ctx = np.eye(CONTEXT_DIM)[0]  # basis vector e_1
    for _ in range(100):
        reward = float(true_theta @ ctx) + rng_data.normal(0, 0.01)
        lints.update("A", reward, context=ctx)
    theta_hat = lints._states["A"].theta_hat
    assert abs(theta_hat[0] - true_theta[0]) < 0.02


def test_state_for_db_round_trip():
    arms = ["A"]
    lints = LinTSSampler(arms, _rng(), prior_var=0.01)
    d = lints.state_for_db("A")
    assert "a_inv" in d and "b" in d
    lints2 = LinTSSampler(["A"], _rng(), prior_var=0.01)
    lints2.restore_from_db("A", d)
    assert np.allclose(lints2._states["A"].a_inv, lints._states["A"].a_inv)
