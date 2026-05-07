"""Unit tests: QuantSettings defaults and overrides."""
from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from unittest.mock import patch


def _fresh_settings(**env_overrides: str):
    """Return a Settings instance with a clean lru_cache and given env overrides."""
    import importlib
    import src.config as cfg_mod

    cfg_mod.get_settings.cache_clear()
    with patch.dict(os.environ, env_overrides, clear=False):
        settings = cfg_mod.Settings()
    return settings


def test_config_quant_defaults():
    s = _fresh_settings()
    assert s.laabh_intraday_mode == "agentic"
    assert s.laabh_quant_bandit_algo == "thompson"
    assert s.laabh_quant_kelly_fraction == 0.5
    assert s.laabh_quant_max_per_trade_pct == 0.03
    assert s.laabh_quant_max_total_exposure_pct == 0.30
    assert s.laabh_quant_lockin_target_pct == 0.05
    assert s.laabh_quant_kill_switch_dd_pct == 0.03
    assert s.laabh_quant_hard_exit_time == time(14, 30)
    assert s.laabh_quant_max_concurrent_positions == 8
    assert s.laabh_quant_universe_size_cap == 20
    assert s.laabh_quant_first_entry_after_minutes == 30
    assert s.laabh_quant_bandit_seed is None
    assert "orb" in s.quant_primitives_list
    assert len(s.quant_primitives_list) == 6


def test_config_quant_overrides():
    s = _fresh_settings(
        LAABH_INTRADAY_MODE="quant",
        LAABH_QUANT_BANDIT_ALGO="lints",
        LAABH_QUANT_KELLY_FRACTION="0.25",
        LAABH_QUANT_BANDIT_SEED="42",
        LAABH_QUANT_PRIMITIVES_ENABLED="orb,momentum",
    )
    assert s.laabh_intraday_mode == "quant"
    assert s.laabh_quant_bandit_algo == "lints"
    assert s.laabh_quant_kelly_fraction == 0.25
    assert s.laabh_quant_bandit_seed == 42
    assert s.quant_primitives_list == ["orb", "momentum"]
