"""Tests for the direction-sign helper used by Stage 2 intraday reattribution.

The function decides which way a v10 conviction points — its output is
multiplied through the intraday (exit - entry) return to produce
``outcome_pnl_pct``. Getting the sign wrong silently flips Stage 2 P&L
attribution, so this is the most load-bearing one-liner in that script.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script_module():
    """Import the script as a module without invoking its __main__ block."""
    spec = importlib.util.spec_from_file_location(
        "reattribute_outcome_z_intraday",
        Path(__file__).resolve().parent.parent
        / "scripts" / "reattribute_outcome_z_intraday.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_positive_conviction_returns_plus_one() -> None:
    mod = _load_script_module()
    assert mod._direction_sign(0.42) == 1
    assert mod._direction_sign(1.0) == 1
    assert mod._direction_sign(0.01) == 1   # no deadband — any positive value


def test_negative_conviction_returns_minus_one() -> None:
    mod = _load_script_module()
    assert mod._direction_sign(-0.42) == -1
    assert mod._direction_sign(-1.0) == -1
    assert mod._direction_sign(-0.01) == -1


def test_zero_conviction_returns_zero() -> None:
    mod = _load_script_module()
    assert mod._direction_sign(0.0) == 0
    assert mod._direction_sign(0) == 0


def test_none_returns_zero() -> None:
    mod = _load_script_module()
    assert mod._direction_sign(None) == 0
