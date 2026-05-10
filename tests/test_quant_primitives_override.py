"""Tests for the ``primitives_override`` mechanism (Phase 4 of the bug audit).

Two layers:
  * ``OrchestratorContext.primitives_override`` — the field exists, defaults
    to None, and is type-list-or-None.
  * ``BacktestRunner._primitives_override`` — defaults to settings list
    minus the structurally-dead primitives (OFI, index_revert), respects
    an explicit override.

End-to-end "orchestrator actually uses the effective list" coverage is in
the smoke run that fires after this phase ships (verifies zero
ofi/index_revert rows in ``backtest_signal_log``).
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from src.quant.backtest.runner import BacktestRunner
from src.quant.context import OrchestratorContext


# ---------------------------------------------------------------------------
# OrchestratorContext field
# ---------------------------------------------------------------------------

def test_context_primitives_override_defaults_to_none():
    """Live default — orchestrator falls back to settings.quant_primitives_list."""
    ctx = OrchestratorContext.live()
    assert ctx.primitives_override is None


def test_context_accepts_explicit_override_list():
    """Field is settable; type system permits list[str]."""
    ctx = OrchestratorContext.live()
    # Build a fresh ctx with override set — dataclass field is mutable post-init
    object.__setattr__(ctx, "primitives_override", ["orb", "vwap_revert"])
    assert ctx.primitives_override == ["orb", "vwap_revert"]


# ---------------------------------------------------------------------------
# BacktestRunner default-override derivation
# ---------------------------------------------------------------------------

def _runner_override(*, primitives_override=None, settings_list=None):
    """Build a runner with a stubbed settings list and read back its override."""
    if settings_list is None:
        settings_list = ["orb", "vwap_revert", "ofi", "vol_breakout", "momentum", "index_revert"]
    with patch("src.quant.backtest.runner.get_settings") as mock_settings:
        mock_settings.return_value.quant_primitives_list = settings_list
        mock_settings.return_value.laabh_quant_backtest_iv_smile_method = "linear"
        runner = BacktestRunner(
            portfolio_id=uuid.uuid4(),
            primitives_override=primitives_override,
        )
        return runner._primitives_override


def test_default_override_drops_dead_primitives_from_settings():
    """No explicit override → derived from settings minus OFI + index_revert."""
    out = _runner_override()
    assert "ofi" not in out
    assert "index_revert" not in out
    # The other 4 should remain in their original order
    assert out == ["orb", "vwap_revert", "vol_breakout", "momentum"]


def test_default_override_handles_settings_without_dead_primitives():
    """If user already disabled OFI in settings, the runner's filter is a no-op."""
    out = _runner_override(settings_list=["orb", "vwap_revert", "momentum"])
    assert out == ["orb", "vwap_revert", "momentum"]


def test_explicit_override_wins_over_default_filtering():
    """User intent (explicit override) is sacrosanct — even if it includes
    one of the dead primitives, we trust the caller (e.g. they may be testing
    an ofi fix)."""
    out = _runner_override(
        primitives_override=["ofi", "vwap_revert"],
        settings_list=["orb", "vwap_revert", "ofi"],
    )
    assert out == ["ofi", "vwap_revert"]


def test_explicit_override_is_copied_not_aliased():
    """Mutating the input list after construction must not change the runner's
    override — defensive, prevents action-at-a-distance bugs."""
    user_list = ["orb", "vwap_revert"]
    with patch("src.quant.backtest.runner.get_settings") as mock_settings:
        mock_settings.return_value.quant_primitives_list = ["orb", "ofi"]
        mock_settings.return_value.laabh_quant_backtest_iv_smile_method = "linear"
        runner = BacktestRunner(
            portfolio_id=uuid.uuid4(),
            primitives_override=user_list,
        )
    user_list.append("evil_inject")
    assert "evil_inject" not in runner._primitives_override


def test_dead_primitives_set_includes_ofi_and_index_revert():
    """Lock the closed set so a future change can't silently re-enable
    a structurally-dead primitive."""
    assert BacktestRunner._BACKTEST_DEAD_PRIMITIVES == frozenset({"ofi", "index_revert"})
