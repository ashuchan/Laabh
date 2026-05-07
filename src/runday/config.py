"""RundaySettings — thresholds and probe targets for the live-day operations tool."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class RundaySettings(BaseSettings):
    """All configurable thresholds for laabh-runday.

    Every assertion in checks/* reads from here — nothing hardcoded.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Connectivity probe targets (overridable for staging)
    runday_nse_probe_symbol: str = "NIFTY"
    runday_dhan_probe_symbol: str = "NIFTY"
    runday_angel_probe_symbol: str = "RELIANCE-EQ"

    # Assertion thresholds used by checkpoint and report
    runday_min_phase1_candidates: int = 30
    runday_min_chain_nse_share_pct: float = 80.0
    runday_max_tier1_latency_ms_p95: int = 3000
    runday_max_tier2_latency_ms_p95: int = 5000
    runday_max_acceptable_missed_pct: float = 5.0
    runday_min_iv_history_coverage_pct: float = 90.0

    # LLM audit expectations
    runday_expected_min_phase3_audit_rows: int = 10  # = FNO_PHASE3_TARGET_OUTPUT

    # Behavior flags
    runday_telegram_on_preflight_ok: bool = True
    runday_pidfile_path: str = "/var/run/laabh.pid"

    # Derived from main settings (read-only via env)
    fno_tier1_size: int = 35
    fno_phase2_target_output: int = 20
    fno_phase3_target_output: int = 10
    fno_phase4_max_open_positions: int = 3
    anthropic_model: str = "claude-sonnet-4-20250514"
    database_url: str = "postgresql+asyncpg://laabh:laabh@localhost:5432/laabh"
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    angel_one_enabled: bool = True
    angel_one_api_key: str = ""
    angel_one_client_id: str = ""
    angel_one_password: str = ""
    angel_one_totp_secret: str = ""
    dhan_pin: str = ""
    dhan_totp_secret: str = ""
    dhan_client_id: str = ""
    dhan_access_token: str = ""
    github_token: str = ""
    github_repo: str = "ashuchan/Laabh"
    nse_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )


@lru_cache(maxsize=1)
def get_runday_settings() -> RundaySettings:
    """Return a cached RundaySettings instance."""
    return RundaySettings()
