"""Application configuration loaded from environment variables (.env)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings loaded from `.env` or environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    database_url: str = Field(
        default="postgresql+asyncpg://laabh:laabh@localhost:5432/laabh",
        alias="DATABASE_URL",
    )
    db_password: str = Field(default="laabh", alias="DB_PASSWORD")

    # --- Angel One ---
    angel_one_api_key: str = Field(default="", alias="ANGEL_ONE_API_KEY")
    angel_one_client_id: str = Field(default="", alias="ANGEL_ONE_CLIENT_ID")
    angel_one_password: str = Field(default="", alias="ANGEL_ONE_PASSWORD")
    angel_one_totp_secret: str = Field(default="", alias="ANGEL_ONE_TOTP_SECRET")

    # --- Anthropic ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="ANTHROPIC_MODEL"
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # --- General ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="Asia/Kolkata", alias="TIMEZONE")
    market_open_time: str = Field(default="09:15", alias="MARKET_OPEN_TIME")
    market_close_time: str = Field(default="15:30", alias="MARKET_CLOSE_TIME")

    # --- F&O Module ---
    fno_module_enabled: bool = Field(default=False, alias="FNO_MODULE_ENABLED")

    # F&O Phase 1 (Universe filter)
    fno_phase1_min_atm_oi: int = Field(default=50000, alias="FNO_PHASE1_MIN_ATM_OI")
    fno_phase1_max_atm_spread_pct: float = Field(default=0.005, alias="FNO_PHASE1_MAX_ATM_SPREAD_PCT")
    fno_phase1_min_avg_volume_5d: int = Field(default=10000, alias="FNO_PHASE1_MIN_AVG_VOLUME_5D")
    fno_phase1_max_days_to_expiry: int = Field(default=3, alias="FNO_PHASE1_MAX_DAYS_TO_EXPIRY")
    fno_phase1_target_output: int = Field(default=50, alias="FNO_PHASE1_TARGET_OUTPUT")

    # F&O Phase 2 (Catalyst scoring)
    fno_phase2_news_lookback_hours: int = Field(default=18, alias="FNO_PHASE2_NEWS_LOOKBACK_HOURS")
    fno_phase2_min_composite_score: float = Field(default=10.0, alias="FNO_PHASE2_MIN_COMPOSITE_SCORE")
    fno_phase2_target_output: int = Field(default=20, alias="FNO_PHASE2_TARGET_OUTPUT")
    fno_phase2_weight_news: float = Field(default=1.0, alias="FNO_PHASE2_WEIGHT_NEWS")
    fno_phase2_weight_sentiment: float = Field(default=1.0, alias="FNO_PHASE2_WEIGHT_SENTIMENT")
    fno_phase2_weight_fii_dii: float = Field(default=0.8, alias="FNO_PHASE2_WEIGHT_FII_DII")
    fno_phase2_weight_macro: float = Field(default=0.8, alias="FNO_PHASE2_WEIGHT_MACRO")
    fno_phase2_weight_convergence: float = Field(default=1.5, alias="FNO_PHASE2_WEIGHT_CONVERGENCE")

    # F&O Phase 3 (Thesis synthesis)
    fno_phase3_target_output: int = Field(default=10, alias="FNO_PHASE3_TARGET_OUTPUT")
    fno_phase3_llm_model: str = Field(
        default="claude-sonnet-4-20250514", alias="FNO_PHASE3_LLM_MODEL"
    )
    fno_phase3_llm_temperature: float = Field(default=0.0, alias="FNO_PHASE3_LLM_TEMPERATURE")

    # F&O Phase 4 (Intraday management)
    fno_phase4_no_entry_before_minutes: int = Field(
        default=30, alias="FNO_PHASE4_NO_ENTRY_BEFORE_MINUTES"
    )
    fno_phase4_hard_exit_time: str = Field(default="14:30", alias="FNO_PHASE4_HARD_EXIT_TIME")
    fno_phase4_oi_recheck_interval_min: int = Field(
        default=30, alias="FNO_PHASE4_OI_RECHECK_INTERVAL_MIN"
    )
    fno_phase4_news_recheck_interval_min: int = Field(
        default=15, alias="FNO_PHASE4_NEWS_RECHECK_INTERVAL_MIN"
    )
    fno_phase4_scale_out_at_pct_gain: float = Field(
        default=0.30, alias="FNO_PHASE4_SCALE_OUT_AT_PCT_GAIN"
    )
    fno_phase4_trailing_stop_from_peak_pct: float = Field(
        default=0.20, alias="FNO_PHASE4_TRAILING_STOP_FROM_PEAK_PCT"
    )
    fno_phase4_max_open_positions: int = Field(default=3, alias="FNO_PHASE4_MAX_OPEN_POSITIONS")
    fno_phase4_cooldown_after_stop_minutes: int = Field(
        default=120, alias="FNO_PHASE4_COOLDOWN_AFTER_STOP_MINUTES"
    )

    # India VIX regime
    fno_vix_low_threshold: float = Field(default=12.0, alias="FNO_VIX_LOW_THRESHOLD")
    fno_vix_high_threshold: float = Field(default=18.0, alias="FNO_VIX_HIGH_THRESHOLD")
    fno_vix_recheck_interval_min: int = Field(default=5, alias="FNO_VIX_RECHECK_INTERVAL_MIN")

    # Position sizing (vol-scaled)
    fno_sizing_risk_per_trade_pct: float = Field(
        default=0.01, alias="FNO_SIZING_RISK_PER_TRADE_PCT"
    )
    fno_sizing_max_position_pct: float = Field(
        default=0.15, alias="FNO_SIZING_MAX_POSITION_PCT"
    )
    fno_sizing_use_atr_scaling: bool = Field(default=True, alias="FNO_SIZING_USE_ATR_SCALING")

    # Strike ranker weights
    fno_ranker_version: str = Field(default="v1", alias="FNO_RANKER_VERSION")
    fno_ranker_w_directional: float = Field(default=0.30, alias="FNO_RANKER_W_DIRECTIONAL")
    fno_ranker_w_convergence: float = Field(default=0.20, alias="FNO_RANKER_W_CONVERGENCE")
    fno_ranker_w_iv_value: float = Field(default=0.15, alias="FNO_RANKER_W_IV_VALUE")
    fno_ranker_w_theta: float = Field(default=0.10, alias="FNO_RANKER_W_THETA")
    fno_ranker_w_oi_structure: float = Field(default=0.15, alias="FNO_RANKER_W_OI_STRUCTURE")
    fno_ranker_w_liquidity: float = Field(default=0.10, alias="FNO_RANKER_W_LIQUIDITY")
    fno_ranker_min_entry_score: float = Field(default=60.0, alias="FNO_RANKER_MIN_ENTRY_SCORE")

    # --- NSE chain source ---
    nse_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        alias="NSE_USER_AGENT",
    )
    nse_request_interval_sec: float = Field(default=2.5, alias="NSE_REQUEST_INTERVAL_SEC")
    nse_cookie_refresh_interval_min: int = Field(
        default=5, alias="NSE_COOKIE_REFRESH_INTERVAL_MIN"
    )
    nse_max_retries: int = Field(default=3, alias="NSE_MAX_RETRIES")

    # --- Dhan chain source ---
    dhan_client_id: str = Field(default="", alias="DHAN_CLIENT_ID")
    dhan_access_token: str = Field(default="", alias="DHAN_ACCESS_TOKEN")
    dhan_request_interval_sec: float = Field(default=3.0, alias="DHAN_REQUEST_INTERVAL_SEC")

    # --- GitHub review-loop issue filer ---
    github_repo: str = Field(default="ashuchan/Laabh", alias="GITHUB_REPO")
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_issue_labels: str = Field(
        default="bug,chain-collector,auto-filed", alias="GITHUB_ISSUE_LABELS"
    )

    # --- Tier policy ---
    fno_tier1_size: int = Field(default=35, alias="FNO_TIER1_SIZE")
    fno_tier2_cadence_min: int = Field(default=15, alias="FNO_TIER2_CADENCE_MIN")
    fno_tier1_cadence_min: int = Field(default=5, alias="FNO_TIER1_CADENCE_MIN")

    # --- Source health policy ---
    fno_source_degrade_after_schema_errors: int = Field(
        default=3, alias="FNO_SOURCE_DEGRADE_AFTER_SCHEMA_ERRORS"
    )
    fno_source_degrade_after_consecutive_errors: int = Field(
        default=10, alias="FNO_SOURCE_DEGRADE_AFTER_CONSECUTIVE_ERRORS"
    )

    # --- NSE-primary feature flag ---
    fno_chain_nse_primary: bool = Field(default=True, alias="FNO_CHAIN_NSE_PRIMARY")

    # --- Risk-free rate for Black-Scholes Greeks ---
    fno_risk_free_rate_pct: float = Field(default=6.5, alias="FNO_RISK_FREE_RATE_PCT")

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic (replace asyncpg driver with psycopg2)."""
        return self.database_url.replace("+asyncpg", "+psycopg2")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
