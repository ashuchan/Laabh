"""Phase 3 boundary tests — LLM-feature cutover safety guarantees.

Plan reference: docs/llm_feature_generator/implementation_plan.md §3.3.

These tests pin the invariants the bandit cutover must NOT break:

  1. Synthetic ``calibrated_conviction=10.0`` (out-of-distribution high)
     does NOT cause the sizer to exceed Kelly + per-trade caps.
  2. The 9-dim LinTS context vector is the 5-dim deterministic vector with
     four LLM dims appended in the documented order.
  3. The per-arm contexts API in :class:`ArmSelector.select` overrides the
     shared context only for the arms supplied.
  4. The persistence dim-mismatch guard cold-starts an arm when the saved
     a_inv shape disagrees with the current sampler's context_dim.
"""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from src.quant.bandit.lints import (
    CONTEXT_DIM,
    CONTEXT_DIM_WITH_LLM,
    LinTSSampler,
    build_context,
    build_context_with_llm,
)
from src.quant.bandit.selector import ArmSelector
from src.quant.sizer import compute_lots


def test_sizer_caps_out_of_distribution_posterior() -> None:
    """OOD-high posterior_mean is bounded by Kelly + max_per_trade_pct.

    posterior_mean=10.0 is far above any realistic value (typical range
    is [-0.05, +0.05]). The sizer must still produce a lot count that
    obeys the per-trade and exposure caps — otherwise an adversarial or
    miscalibrated LLM dimension could blow up sizing.
    """
    lots = compute_lots(
        posterior_mean=10.0,
        portfolio_capital=Decimal("100000"),
        max_loss_per_lot=Decimal("1000"),
        estimated_costs=Decimal("250"),
        expected_gross_pnl=Decimal("3000"),
        open_exposure=Decimal("0"),
        lockin_active=False,
        kelly_fraction=0.5,
        max_per_trade_pct=0.03,
        lockin_size_reduction=0.5,
        max_total_exposure_pct=0.30,
        cost_gate_multiple=3.0,
    )
    # Hard ceiling: capital × max_per_trade_pct / max_loss_per_lot
    #   = 100_000 × 0.03 / 1000 = 3 lots
    assert lots <= 3, f"sizer exceeded per-trade cap: {lots} lots"
    assert lots >= 0


def test_context_with_llm_appends_in_order() -> None:
    """build_context_with_llm = base ++ [conviction, durability, specificity, risk_flag]."""
    base = build_context(
        vix_value=15.0,
        time_of_day_pct=0.5,
        day_running_pnl_pct=0.0,
        nifty_5d_return=0.0,
        realized_vol_30min_pctile=0.5,
    )
    augmented = build_context_with_llm(
        vix_value=15.0,
        time_of_day_pct=0.5,
        day_running_pnl_pct=0.0,
        nifty_5d_return=0.0,
        realized_vol_30min_pctile=0.5,
        llm_calibrated_conviction=0.3,
        llm_thesis_durability=0.7,
        llm_catalyst_specificity=0.8,
        llm_risk_flag=-0.2,
    )
    assert augmented.shape == (CONTEXT_DIM_WITH_LLM,)
    np.testing.assert_array_equal(augmented[:CONTEXT_DIM], base)
    np.testing.assert_allclose(augmented[CONTEXT_DIM:], [0.3, 0.7, 0.8, -0.2])


def test_per_arm_contexts_override_shared() -> None:
    """Passing per-arm contexts must override the shared context per-arm.

    With one arm pinned to a 'good' context and another to a 'bad' one,
    the sampler should consistently prefer the good arm under deterministic
    seeding.
    """
    rng = np.random.default_rng(42)
    sampler = LinTSSampler(["A", "B"], rng, prior_var=0.01, context_dim=2)

    # Inject a known posterior bias so the prediction is deterministic
    # before any updates: θ̂ is initialised to zeros, so any per-arm
    # difference must come from the contexts.
    sampler.update("A", 1.0, context=np.array([1.0, 0.0]))
    sampler.update("B", 1.0, context=np.array([0.0, 1.0]))

    # Arm A's posterior aligns with first-dim contexts; B aligns with second-dim.
    wins_a = wins_b = 0
    for _ in range(50):
        chosen = sampler.select(
            ["A", "B"],
            context=np.zeros(2),                            # shared (ignored)
            contexts={"A": np.array([1.0, 0.0]), "B": np.array([0.0, 1.0])},
        )
        if chosen == "A":
            wins_a += 1
        else:
            wins_b += 1
    # Both arms should win in their own context, so neither should be ≤5%.
    assert min(wins_a, wins_b) > 5, f"per-arm context override broke: A={wins_a} B={wins_b}"


def test_persistence_skips_warm_restore_on_dim_mismatch() -> None:
    """If a 5×5 a_inv is restored into a 9-dim sampler, cold-start instead."""
    from src.quant.persistence import _patch_posterior

    selector = ArmSelector(["X_orb"], algo="lints", context_dim=CONTEXT_DIM_WITH_LLM)
    # Saved state from a previous mode='gate' day — dim=5.
    saved_a_inv = (np.eye(CONTEXT_DIM) * 0.01).tolist()
    saved_b = np.zeros(CONTEXT_DIM).tolist()

    _patch_posterior(
        selector, "X_orb",
        mean=0.0, var=0.01,
        a_inv=saved_a_inv, b_vector=saved_b, gamma=0.95,
    )

    # State at "X_orb" must still be the cold-start dim-9 matrix — not
    # overwritten by the saved 5×5 (which would silently break selection).
    impl_state = selector._impl._states["X_orb"]
    assert impl_state.a_inv.shape == (CONTEXT_DIM_WITH_LLM, CONTEXT_DIM_WITH_LLM)
    assert impl_state.b.shape == (CONTEXT_DIM_WITH_LLM,)


def test_posterior_var_for_context_returns_xt_ainv_x() -> None:
    """LinTS contextual variance must equal x^T A_inv x for the given arm.

    This is the quantity the reserved-slot gate now uses to rank arms by
    exploration value (review fix P3 #6). The mean-of-diagonal proxy
    (``posterior_var(arm)``) is left as a separate method.
    """
    rng = np.random.default_rng(0)
    sampler = LinTSSampler(["A", "B"], rng, prior_var=0.01, context_dim=3)
    # Update arm A heavily so its A_inv shrinks (precision rises).
    for _ in range(20):
        sampler.update("A", 0.5, context=np.array([1.0, 1.0, 1.0]))
    x = np.array([1.0, 0.0, 0.0])
    var_a = sampler.posterior_var_for_context("A", x)
    var_b = sampler.posterior_var_for_context("B", x)
    # A was updated; B is at the prior. A's contextual variance should be
    # smaller (more precision) than B's for the same context.
    assert var_a < var_b
    # And the value should match the closed-form x^T A_inv x.
    expected_a = float(x @ sampler._states["A"].a_inv @ x)
    assert abs(var_a - expected_a) < 1e-12


def test_arm_selector_routes_contextual_var_to_lints() -> None:
    """ArmSelector.posterior_var_for_context delegates correctly under LinTS."""
    selector = ArmSelector(["A_orb"], algo="lints", context_dim=5)
    x = np.array([0.5, 0.5, 0.5, 0.5, 0.5])
    v = selector.posterior_var_for_context("A_orb", x)
    assert isinstance(v, float)
    assert v >= 0   # quadratic form on a PSD matrix


def test_arm_selector_thompson_fallback() -> None:
    """Thompson has no context — the helper falls back to mean-of-state."""
    import numpy as _np
    selector = ArmSelector(["A_orb"], algo="thompson")
    # Pass anything as context — Thompson should ignore it cleanly.
    v = selector.posterior_var_for_context("A_orb", _np.zeros(5))
    assert isinstance(v, float)
