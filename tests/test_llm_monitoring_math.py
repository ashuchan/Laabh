"""Coverage for math helpers in src.fno.llm_monitoring.

Specifically:
  - ``_max_drawdown`` — peak-to-trough on the cumulative P&L equity curve.
  - ``_sharpe`` — annualised Sharpe from per-trade P&L.
  - ``_safe`` — defensive numeric coercion.

The async DB-bound helpers (three_way_sharpe_compare,
cost_per_trade_compare, etc.) are smoke-tested manually and via the
dashboard; they need Postgres.
"""
from __future__ import annotations

import math

from src.fno.llm_monitoring import _max_drawdown, _safe, _sharpe


# ---------------------------------------------------------------------------
# _max_drawdown
# ---------------------------------------------------------------------------


def test_drawdown_insufficient_data_returns_none() -> None:
    """< 5 observations → not enough signal."""
    assert _max_drawdown([100, 100, 100]) is None


def test_drawdown_strictly_winning_is_none_or_zero() -> None:
    """A monotonically winning curve has no drawdown."""
    dd = _max_drawdown([10, 10, 10, 10, 10])
    assert dd is None or dd == 0.0


def test_drawdown_classic_peak_to_trough() -> None:
    """5 wins of 100, then 3 losses of 200 each → equity 500 → -100.
    Drawdown = 600 / max(peak, 1) ≈ 1.2 (full anchor at 500)."""
    pnls = [100, 100, 100, 100, 100] + [-200, -200, -200]
    dd = _max_drawdown(pnls)
    assert dd is not None
    assert dd > 1.0


def test_drawdown_immediate_loss_then_recovery() -> None:
    """Equity goes negative immediately then recovers — drawdown captured."""
    pnls = [-50, -50, 100, 100, 50, 50, 50, 50]
    dd = _max_drawdown(pnls)
    assert dd is not None
    assert dd > 0


# ---------------------------------------------------------------------------
# _sharpe
# ---------------------------------------------------------------------------


def test_sharpe_insufficient_data_returns_none() -> None:
    assert _sharpe([1.0, 2.0, 3.0]) is None


def test_sharpe_zero_variance_returns_none() -> None:
    """Constant returns have zero std — Sharpe is undefined."""
    assert _sharpe([1.0] * 10) is None


def test_sharpe_positive_on_profitable_series() -> None:
    pnls = [100, 90, 110, 100, 95, 105, 100, 100, 100, 100]
    s = _sharpe(pnls)
    assert s is not None
    assert s > 0


def test_sharpe_negative_on_losing_series() -> None:
    pnls = [-100, -90, -110, -100, -95, -105, -100, -100, -100, -100]
    s = _sharpe(pnls)
    assert s is not None
    assert s < 0


def test_sharpe_annualisation_factor() -> None:
    """Verify the sqrt(252) annualisation matches the expected formula."""
    # Mean = 0.5, std = 0.5 → daily Sharpe = 1.0 → annualised ≈ 15.87.
    pnls = [1.0, 0.0] * 10
    s = _sharpe(pnls)
    assert s is not None
    assert abs(s - math.sqrt(252)) < 0.5


# ---------------------------------------------------------------------------
# _safe
# ---------------------------------------------------------------------------


def test_safe_none_returns_none() -> None:
    assert _safe(None) is None


def test_safe_non_finite_returns_none() -> None:
    assert _safe(float("inf")) is None
    assert _safe(float("nan")) is None


def test_safe_int_to_float() -> None:
    assert _safe(5) == 5.0


def test_safe_unparseable_string_returns_none() -> None:
    assert _safe("not a number") is None
