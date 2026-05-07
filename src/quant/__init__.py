"""Quant module — bandit-orchestrated intraday F&O trading mode.

Activated when LAABH_INTRADAY_MODE=quant. All LLM intraday agents are
bypassed; sizing, exits, and trade decisions are driven by this module.
"""
