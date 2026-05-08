"""Tests for Thompson Sampling bandit."""
from __future__ import annotations

import numpy as np
import pytest

from src.quant.bandit.posterior import PosteriorState
from src.quant.bandit.thompson import ThompsonSampler


def _rng(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def test_select_returns_known_arm():
    arms = ["NIFTY_orb", "RELIANCE_vwap_revert"]
    ts = ThompsonSampler(arms, _rng())
    result = ts.select(arms)
    assert result in arms


def test_select_none_on_empty():
    ts = ThompsonSampler(["A"], _rng())
    assert ts.select([]) is None


def test_select_none_on_unknown_arms():
    ts = ThompsonSampler(["A"], _rng())
    assert ts.select(["Z"]) is None


def test_posterior_mean_converges():
    """Mean converges close to true mean after 50 observations (fixed seed)."""
    true_mean = 0.02
    arms = ["arm_a"]
    ts = ThompsonSampler(arms, _rng(42), prior_mean=0.0, prior_var=0.01, obs_var=0.01)
    rng_data = np.random.default_rng(1)
    for _ in range(50):
        reward = rng_data.normal(true_mean, 0.1)
        ts.update("arm_a", reward)
    post_mean = ts.posterior_mean("arm_a")
    assert abs(post_mean - true_mean) < 0.02  # within 2% of true mean


def test_snapshot_restore_round_trip():
    arms = ["A", "B"]
    ts = ThompsonSampler(arms, _rng(7))
    ts.update("A", 0.05)
    ts.update("B", -0.01)
    snap = ts.snapshot()
    ts.update("A", 0.10)
    ts.restore(snap)
    # After restore, posterior_mean matches snapshot
    assert ts.posterior_mean("A") == pytest.approx(snap["A"].mean)
    assert ts.posterior_mean("B") == pytest.approx(snap["B"].mean)


def test_forget_widens_variance():
    ts = ThompsonSampler(["A"], _rng(), prior_var=0.01)
    var_before = ts.posterior_var("A")
    ts.apply_forget(0.95)
    var_after = ts.posterior_var("A")
    assert var_after == pytest.approx(var_before / 0.95, rel=1e-6)


def test_selection_deterministic_given_seed():
    arms = ["A", "B", "C"]
    ts1 = ThompsonSampler(arms, _rng(99))
    ts2 = ThompsonSampler(arms, _rng(99))
    r1 = ts1.select(arms)
    r2 = ts2.select(arms)
    assert r1 == r2


def test_n_obs_starts_at_zero_and_increments():
    ts = ThompsonSampler(["A", "B"], _rng())
    assert ts.n_obs("A") == 0
    assert ts.n_obs("B") == 0
    ts.update("A", 0.05)
    assert ts.n_obs("A") == 1
    assert ts.n_obs("B") == 0
    ts.update("A", 0.05)
    assert ts.n_obs("A") == 2


def test_n_obs_unknown_arm_returns_zero():
    ts = ThompsonSampler(["A"], _rng())
    assert ts.n_obs("Z") == 0


def test_prefer_higher_reward_arm():
    """After many updates, sampler should prefer the arm with higher true mean."""
    arms = ["good", "bad"]
    ts = ThompsonSampler(arms, _rng(0), prior_var=0.001, obs_var=0.001)
    rng_data = np.random.default_rng(2)
    for _ in range(100):
        ts.update("good", rng_data.normal(0.05, 0.01))
        ts.update("bad", rng_data.normal(-0.02, 0.01))
    assert ts.posterior_mean("good") > ts.posterior_mean("bad")
