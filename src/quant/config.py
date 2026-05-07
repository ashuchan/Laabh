"""Quant-mode configuration — typed accessors for LAABH_QUANT_* settings.

Thin module that re-exports quant-relevant fields so quant submodules can
import a single focused namespace instead of the full Settings object.
"""
from __future__ import annotations

from datetime import time
from typing import Literal

from src.config import get_settings


def get_quant_config() -> "QuantConfig":
    """Return a QuantConfig view of the current settings (cheap, no copy)."""
    return QuantConfig(get_settings())


class QuantConfig:
    """Read-only view of LAABH_QUANT_* fields, constructed from Settings."""

    __slots__ = ("_s",)

    def __init__(self, settings) -> None:
        self._s = settings

    @property
    def poll_interval_sec(self) -> int:
        return self._s.laabh_quant_poll_interval_sec

    @property
    def primitives(self) -> list[str]:
        return self._s.quant_primitives_list

    @property
    def min_signal_strength(self) -> float:
        return self._s.laabh_quant_min_signal_strength

    @property
    def bandit_algo(self) -> Literal["thompson", "lints"]:
        return self._s.laabh_quant_bandit_algo

    @property
    def forget_factor(self) -> float:
        return self._s.laabh_quant_bandit_forget_factor

    @property
    def prior_mean(self) -> float:
        return self._s.laabh_quant_bandit_prior_mean

    @property
    def prior_var(self) -> float:
        return self._s.laabh_quant_bandit_prior_var

    @property
    def hard_exit_time(self) -> time:
        return self._s.laabh_quant_hard_exit_time

    @property
    def universe_size_cap(self) -> int:
        return self._s.laabh_quant_universe_size_cap

    @property
    def max_concurrent_positions(self) -> int:
        return self._s.laabh_quant_max_concurrent_positions
