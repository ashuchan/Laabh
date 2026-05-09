"""Tests for the metrics module.

Each metric is verified against either a hand-computed reference, a
known-property synthetic series, or a self-consistency check (e.g. flat
returns ⇒ zero Sharpe).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.quant.backtest.reporting.metrics import (
    MetricsBundle,
    bootstrap_sharpe_ci,
    calmar,
    compute_metrics,
    deflated_sharpe,
    kurtosis_excess,
    max_drawdown,
    mean,
    median,
    profit_factor,
    sharpe,
    skew,
    stdev,
    win_rate,
    avg_win,
    avg_loss,
    _norm_cdf,
    _norm_inv,
)
from src.quant.backtest.reporting.per_arm import ArmStats, per_arm_stats


# ---------------------------------------------------------------------------
# Distributional moments
# ---------------------------------------------------------------------------

def test_mean_simple():
    assert mean([1, 2, 3, 4]) == pytest.approx(2.5)


def test_mean_empty_returns_zero():
    assert mean([]) == 0.0


def test_median_odd_count():
    assert median([1, 2, 3]) == 2


def test_median_even_count():
    assert median([1, 2, 3, 4]) == 2.5


def test_stdev_sample_correction():
    # σ_pop = sqrt(2); sample σ with n-1 = sqrt(2.5) ≈ 1.581
    assert stdev([1, 2, 3, 4, 5]) == pytest.approx(math.sqrt(2.5), abs=1e-9)


def test_stdev_zero_for_constant():
    assert stdev([5, 5, 5, 5]) == 0.0


def test_skew_symmetric_zero():
    """Symmetric data has skew ~0."""
    s = skew([-2, -1, 0, 1, 2])
    assert s == pytest.approx(0.0, abs=1e-9)


def test_skew_right_tailed_positive():
    # Heavily right-tailed
    s = skew([1, 1, 1, 1, 100])
    assert s > 0.5


def test_kurtosis_excess_normal_approx_zero():
    # Approx normal data → excess kurt ~0 (sample noise allowed)
    import random
    rng = random.Random(123)
    xs = [rng.gauss(0, 1) for _ in range(2000)]
    assert abs(kurtosis_excess(xs)) < 0.5


def test_kurtosis_excess_uniform_negative():
    # Uniform distribution has excess kurt = -1.2
    import random
    rng = random.Random(123)
    xs = [rng.uniform(-1, 1) for _ in range(5000)]
    assert kurtosis_excess(xs) < -0.5


# ---------------------------------------------------------------------------
# Sharpe
# ---------------------------------------------------------------------------

def test_sharpe_zero_for_zero_mean():
    sr = sharpe([0.01, -0.01, 0.01, -0.01])  # mean 0
    assert sr == 0.0


def test_sharpe_positive_for_consistent_gains():
    sr = sharpe([0.001] * 10 + [0.002] * 10)
    assert sr > 0


def test_sharpe_annualisation_252():
    # Constant 1bp/day return; std=0 → Sharpe undefined → 0 by convention
    assert sharpe([0.0001] * 10) == 0.0


def test_sharpe_empty_returns_zero():
    assert sharpe([]) == 0.0


# ---------------------------------------------------------------------------
# Deflated Sharpe — robustness checks
# ---------------------------------------------------------------------------

def test_deflated_sharpe_high_for_strong_consistent_signal():
    """A long series with consistent positive returns should yield DSR > 0.5."""
    import random
    rng = random.Random(42)
    xs = [0.001 + rng.gauss(0, 0.005) for _ in range(252)]
    dsr = deflated_sharpe(xs, n_trials=1)
    assert dsr > 0.5


def test_deflated_sharpe_drops_with_more_trials():
    """Multiple-testing inflation: same data, more trials → lower DSR."""
    import random
    rng = random.Random(42)
    xs = [0.001 + rng.gauss(0, 0.005) for _ in range(252)]
    dsr_1 = deflated_sharpe(xs, n_trials=1)
    dsr_100 = deflated_sharpe(xs, n_trials=100)
    assert dsr_100 < dsr_1


def test_deflated_sharpe_short_series_returns_zero():
    assert deflated_sharpe([0.01, 0.02], n_trials=1) == 0.0


def test_deflated_sharpe_in_unit_interval():
    """DSR is a probability; must be in [0, 1]."""
    import random
    rng = random.Random(7)
    xs = [rng.gauss(0.001, 0.01) for _ in range(60)]
    dsr = deflated_sharpe(xs, n_trials=10)
    assert 0.0 <= dsr <= 1.0


# ---------------------------------------------------------------------------
# Drawdown / Calmar
# ---------------------------------------------------------------------------

def test_max_drawdown_monotonic_zero():
    assert max_drawdown([0.01, 0.01, 0.01]) == 0.0


def test_max_drawdown_simple_case():
    # nav: 1.0 → 1.10 → 0.99 → 1.05; peak 1.10, trough 0.99 → dd ≈ 0.10
    xs = [0.10, -0.10, 0.06]
    dd = max_drawdown(xs)
    assert dd == pytest.approx(0.10, abs=0.01)


def test_max_drawdown_empty():
    assert max_drawdown([]) == 0.0


def test_calmar_zero_when_no_drawdown():
    assert calmar([0.001, 0.001]) == 0.0


def test_calmar_zero_for_empty():
    assert calmar([]) == 0.0


def test_calmar_positive_for_realistic_series():
    # Drift up with one drawdown
    xs = [0.005] * 100 + [-0.05] + [0.005] * 100
    c = calmar(xs)
    assert c > 0.0


# ---------------------------------------------------------------------------
# Win rate / profit factor
# ---------------------------------------------------------------------------

def test_win_rate_basic():
    assert win_rate([0.01, -0.01, 0.02, -0.02, 0.0]) == pytest.approx(0.4)


def test_profit_factor_basic():
    # wins: 0.03, 0.04 → 0.07; losses: -0.02, -0.01 → 0.03 → PF = 7/3
    assert profit_factor([0.03, -0.02, 0.04, -0.01]) == pytest.approx(7 / 3, abs=1e-9)


def test_profit_factor_no_losses_with_wins_returns_inf():
    assert profit_factor([0.01, 0.02, 0.03]) == float("inf")


def test_profit_factor_empty_returns_zero():
    assert profit_factor([]) == 0.0


def test_avg_win_avg_loss():
    xs = [0.01, -0.02, 0.03, -0.04]
    assert avg_win(xs) == pytest.approx(0.02)
    assert avg_loss(xs) == pytest.approx(-0.03)


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------

def test_bootstrap_ci_contains_point_estimate_for_long_series():
    import random
    rng = random.Random(42)
    xs = [0.001 + rng.gauss(0, 0.01) for _ in range(252)]
    sr = sharpe(xs)
    lo, hi = bootstrap_sharpe_ci(xs, n_iter=200, block_size=5, seed=42)
    assert lo <= sr <= hi


def test_bootstrap_ci_lower_le_upper():
    xs = [0.001] * 100
    lo, hi = bootstrap_sharpe_ci(xs, n_iter=100, seed=42)
    assert lo <= hi


def test_bootstrap_ci_seed_reproducible():
    xs = [0.001 + 0.0001 * i for i in range(40)]
    a = bootstrap_sharpe_ci(xs, n_iter=100, seed=42)
    b = bootstrap_sharpe_ci(xs, n_iter=100, seed=42)
    assert a == b


# ---------------------------------------------------------------------------
# compute_metrics — composition
# ---------------------------------------------------------------------------

def test_compute_metrics_returns_metrics_bundle():
    import random
    rng = random.Random(11)
    xs = [rng.gauss(0.001, 0.01) for _ in range(50)]
    bundle = compute_metrics(xs, bootstrap_iter=100)
    assert isinstance(bundle, MetricsBundle)
    assert bundle.n == 50
    assert bundle.std > 0
    assert bundle.sharpe_ci_lower <= bundle.sharpe_ci_upper


def test_compute_metrics_empty_series():
    bundle = compute_metrics([], bootstrap_iter=10)
    assert bundle.n == 0
    assert bundle.sharpe == 0.0
    assert bundle.deflated_sharpe == 0.0
    assert bundle.max_drawdown == 0.0


# ---------------------------------------------------------------------------
# Normal CDF / inverse — internal correctness
# ---------------------------------------------------------------------------

def test_norm_cdf_at_zero_is_half():
    assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-12)


def test_norm_cdf_far_left_is_near_zero():
    assert _norm_cdf(-5.0) == pytest.approx(0.0, abs=1e-6)


def test_norm_cdf_far_right_is_near_one():
    assert _norm_cdf(5.0) == pytest.approx(1.0, abs=1e-6)


def test_norm_inv_round_trips():
    for p in [0.05, 0.25, 0.5, 0.75, 0.95]:
        x = _norm_inv(p)
        assert _norm_cdf(x) == pytest.approx(p, abs=1e-3)


def test_norm_inv_clamps_at_edges():
    assert _norm_inv(0.0) == -8.0
    assert _norm_inv(1.0) == 8.0


# ---------------------------------------------------------------------------
# per_arm
# ---------------------------------------------------------------------------

class _T:
    """Tiny trade fixture matching the per-arm Protocol."""

    def __init__(self, arm_id, pnl, entry_min, exit_min):
        self.arm_id = arm_id
        self.realized_pnl = Decimal(str(pnl)) if pnl is not None else None
        self.entry_at = datetime(2026, 4, 27, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=entry_min)
        self.exit_at = (
            datetime(2026, 4, 27, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=exit_min)
            if exit_min is not None else None
        )


def test_per_arm_groups_by_arm_id():
    trades = [
        _T("A_orb", 100, 0, 30),
        _T("A_orb", -50, 30, 60),
        _T("B_vwap", 75, 0, 45),
    ]
    out = per_arm_stats(trades)
    arms = {s.arm_id: s for s in out}
    assert "A_orb" in arms
    assert "B_vwap" in arms
    assert arms["A_orb"].trade_count == 2
    assert arms["A_orb"].pnl_total == pytest.approx(50.0)
    assert arms["B_vwap"].trade_count == 1


def test_per_arm_excludes_unrealised_trades():
    trades = [
        _T("A_orb", 100, 0, 30),
        _T("A_orb", None, 30, None),  # still open
    ]
    out = per_arm_stats(trades)
    assert out[0].trade_count == 1


def test_per_arm_sorted_by_pnl_desc():
    trades = [
        _T("LOSER", -100, 0, 30),
        _T("WINNER", 200, 0, 30),
        _T("MIDDLE", 50, 0, 30),
    ]
    out = per_arm_stats(trades)
    assert [s.arm_id for s in out] == ["WINNER", "MIDDLE", "LOSER"]


def test_per_arm_empty_input():
    assert per_arm_stats([]) == []


def test_per_arm_holding_period_minutes():
    trades = [_T("A", 100, 0, 45), _T("A", 50, 60, 90)]
    out = per_arm_stats(trades)
    # Avg holding = (45 + 30) / 2 = 37.5
    assert out[0].avg_holding_minutes == pytest.approx(37.5, abs=0.01)
