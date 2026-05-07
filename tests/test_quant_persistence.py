"""Tests for persistence helpers (no DB required — test pure logic)."""
from __future__ import annotations

import pytest

from src.quant.persistence import _split_arm


@pytest.mark.parametrize("arm_id,expected", [
    ("RELIANCE_orb", ("RELIANCE", "orb")),
    ("NIFTY_vwap_revert", ("NIFTY", "vwap_revert")),
    ("BANKNIFTY_vol_breakout", ("BANKNIFTY", "vol_breakout")),
    ("HDFCBANK_momentum", ("HDFCBANK", "momentum")),
    ("ICICIBANK_ofi", ("ICICIBANK", "ofi")),
    ("NIFTY_index_revert", ("NIFTY", "index_revert")),
])
def test_split_arm(arm_id, expected):
    assert _split_arm(arm_id) == expected


def test_posterior_patch_roundtrip():
    """Patch and read back posterior mean/var through ThompsonSampler."""
    import numpy as np
    from src.quant.bandit.selector import ArmSelector
    from src.quant.persistence import _patch_posterior

    arms = ["RELIANCE_orb", "NIFTY_momentum"]
    selector = ArmSelector(arms, prior_mean=0.0, prior_var=0.01, seed=0)
    _patch_posterior(selector, "RELIANCE_orb", mean=0.05, var=0.02)

    assert selector.posterior_mean("RELIANCE_orb") == pytest.approx(0.05)
    assert selector.posterior_var("RELIANCE_orb") == pytest.approx(0.02)


def test_forget_factor_applied():
    """γ applied to variance: var_after = var_before / γ."""
    from src.quant.bandit.selector import ArmSelector

    arms = ["A_orb"]
    selector = ArmSelector(arms, prior_mean=0.0, prior_var=0.01, seed=0)
    var_before = selector.posterior_var("A_orb")
    selector.apply_forget(0.95)
    var_after = selector.posterior_var("A_orb")
    assert var_after == pytest.approx(var_before / 0.95, rel=1e-6)
