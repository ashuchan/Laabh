"""Quant-mode configuration — thin re-export from the main Settings object.

All LAABH_QUANT_* vars live in src.config.Settings. This module provides
a typed accessor so quant submodules can import without pulling in the full
Settings class, and keeps the public API stable if config layout changes.
"""
from __future__ import annotations

from src.config import Settings, get_settings


def get_quant_settings() -> Settings:
    """Return the shared settings instance (cached singleton)."""
    return get_settings()
