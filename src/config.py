"""Application configuration loaded from environment variables (.env)."""
from __future__ import annotations

from datetime import time
from functools import lru_cache
from typing import Literal

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
    # Set ANGEL_ONE_ENABLED=false to skip the WebSocket stream and the
    # preflight Angel One check entirely (Dhan-only deployment).
    angel_one_enabled: bool = Field(default=True, alias="ANGEL_ONE_ENABLED")
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

    # --- Whisper pipeline (Phase 3 podcast transcription) ---
    # Empty = whisper jobs skipped at scheduler boot.
    whisper_model: str = Field(default="", alias="WHISPER_MODEL")
    whisper_device: str = Field(default="", alias="WHISPER_DEVICE")
    whisper_data_dir: str = Field(default="/data/whisper", alias="WHISPER_DATA_DIR")
    audio_retention_days: int = Field(default=7, alias="AUDIO_RETENTION_DAYS")

    # --- F&O Module ---
    fno_module_enabled: bool = Field(default=False, alias="FNO_MODULE_ENABLED")

    # F&O Phase 1 (Universe filter)
    # Calibrated 2026-05-08 against live ATM-OI distribution across the
    # 215-instrument F&O universe (median 826, p75 1.6k, max 335k for NIFTY).
    # The previous 50k/5k pair was tuned on EOD-settled OI and rejected ~96%
    # of names against intraday snapshots. New pair admits the top ~95
    # instruments (~44%) feeding Phase 2 a useful candidate pool.
    fno_phase1_min_atm_oi: int = Field(default=5000, alias="FNO_PHASE1_MIN_ATM_OI")
    # Mid-cap (Tier 2) OI threshold — Tier 2 underlyings have ~10x lower
    # ATM OI than Tier 1 large-caps, so a single Nifty-50-calibrated
    # threshold rejects them all. Use a lower bar for Tier 2.
    fno_phase1_min_atm_oi_tier2: int = Field(default=1000, alias="FNO_PHASE1_MIN_ATM_OI_TIER2")
    # Spread = (ask-bid)/mid as decimal. Real-world ATM bid-ask on liquid
    # index/large-cap options is 0.1-2%; mid-caps run 2-5%. Default 0.05 (5%)
    # admits everything except genuinely illiquid contracts.
    fno_phase1_max_atm_spread_pct: float = Field(default=0.05, alias="FNO_PHASE1_MAX_ATM_SPREAD_PCT")
    # Tier 1 large-caps face a tighter 2% bar; tier 2 keeps the 5% bar.
    fno_phase1_max_atm_spread_pct_tier1: float = Field(default=0.02, alias="FNO_PHASE1_MAX_ATM_SPREAD_PCT_TIER1")
    fno_phase1_min_avg_volume_5d: int = Field(default=10000, alias="FNO_PHASE1_MIN_AVG_VOLUME_5D")
    fno_phase1_max_days_to_expiry: int = Field(default=3, alias="FNO_PHASE1_MAX_DAYS_TO_EXPIRY")
    fno_phase1_target_output: int = Field(default=50, alias="FNO_PHASE1_TARGET_OUTPUT")

    # F&O Phase 2 (Catalyst scoring)
    fno_phase2_news_lookback_hours: int = Field(default=18, alias="FNO_PHASE2_NEWS_LOOKBACK_HOURS")
    # Composite is bounded [0, 10]. The original 6.0 calibration was tuned
    # on the synthetic April 30 chain when most dimensions were neutral
    # defaults (5.0). With the smoother convergence rule and per-stock
    # FII/DII alignment landed 2026-05-08, the realistic ceiling for a
    # "real but not screaming" signal sits around 5.5-6.0 — admitting at
    # 5.5 lets the LLM gate (the actual edge filter) see candidates
    # instead of pre-filtering them out arithmetically.
    fno_phase2_min_composite_score: float = Field(default=5.5, alias="FNO_PHASE2_MIN_COMPOSITE_SCORE")
    fno_phase2_target_output: int = Field(default=20, alias="FNO_PHASE2_TARGET_OUTPUT")
    fno_phase2_weight_news: float = Field(default=1.0, alias="FNO_PHASE2_WEIGHT_NEWS")
    fno_phase2_weight_sentiment: float = Field(default=1.0, alias="FNO_PHASE2_WEIGHT_SENTIMENT")
    fno_phase2_weight_fii_dii: float = Field(default=0.8, alias="FNO_PHASE2_WEIGHT_FII_DII")
    fno_phase2_weight_macro: float = Field(default=0.8, alias="FNO_PHASE2_WEIGHT_MACRO")
    fno_phase2_weight_convergence: float = Field(default=1.5, alias="FNO_PHASE2_WEIGHT_CONVERGENCE")

    # F&O Sentiment collector (writes raw_content[media_type='sentiment'])
    # Composite of VIX (fear gauge) + multi-horizon NIFTY/breadth trend.
    # See src/collectors/sentiment_collector.py for the formula.
    fno_sentiment_weight_vix: float = Field(default=0.20, alias="FNO_SENTIMENT_WEIGHT_VIX")
    fno_sentiment_weight_1d: float = Field(default=0.30, alias="FNO_SENTIMENT_WEIGHT_1D")
    fno_sentiment_weight_1w: float = Field(default=0.25, alias="FNO_SENTIMENT_WEIGHT_1W")
    fno_sentiment_weight_1m: float = Field(default=0.25, alias="FNO_SENTIMENT_WEIGHT_1M")
    # Multiplier applied to the 1-day weight when the most recent trading day
    # is more than 1 calendar day behind today (Mondays + post-holiday).
    # The 1d signal is fresher mid-week than after a weekend, so we discount it.
    fno_sentiment_stale_1d_decay: float = Field(default=0.5, alias="FNO_SENTIMENT_STALE_1D_DECAY")
    # Min instruments with both `t` and `t-N` closes for the breadth leg of a
    # horizon to be valid. Below this, the breadth leg drops out for that horizon.
    fno_sentiment_min_breadth_instruments: int = Field(
        default=50, alias="FNO_SENTIMENT_MIN_BREADTH_INSTRUMENTS"
    )
    # Max age of the latest vix_ticks row before we fall back to yfinance.
    fno_sentiment_vix_max_stale_hours: int = Field(
        default=24, alias="FNO_SENTIMENT_VIX_MAX_STALE_HOURS"
    )
    # Symbol of the index instrument used as the trend-leg benchmark. The
    # bootstrap creates 'NIFTY' from the F&O bhavcopy without price history;
    # the original index seed populates 'NIFTY 50' with prices, so the
    # default points at the latter.
    fno_sentiment_index_symbol: str = Field(
        default="NIFTY 50", alias="FNO_SENTIMENT_INDEX_SYMBOL"
    )

    # F&O Phase 3 (Thesis synthesis)
    fno_phase3_target_output: int = Field(default=30, alias="FNO_PHASE3_TARGET_OUTPUT")
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
    # TOTP-based programmatic login. When DHAN_PIN + DHAN_TOTP_SECRET are set,
    # src.auth.dhan_token mints a fresh 24h access token automatically and
    # DHAN_ACCESS_TOKEN above becomes a manual override only.
    dhan_pin: str = Field(default="", alias="DHAN_PIN")
    dhan_totp_secret: str = Field(default="", alias="DHAN_TOTP_SECRET")
    dhan_request_interval_sec: float = Field(default=3.0, alias="DHAN_REQUEST_INTERVAL_SEC")
    # Comma-separated "OLD=NEW" pairs to remap a universe symbol to the trading
    # symbol Dhan's instrument master actually publishes. Use this to handle
    # post-corporate-action renames (e.g. demergers) without code changes.
    # Example: "TATAMOTORS=TATAMOTORS-EQ,IDEA=IDEA-EQ"
    dhan_symbol_aliases: str = Field(default="", alias="DHAN_SYMBOL_ALIASES")

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

    # --- Equity Trading master switch ---
    # When False, every code path that would create or trigger an EQUITY
    # (non-F&O) paper trade is refused: the API endpoint returns 403, the
    # LLM strategy jobs are not registered with the scheduler, pending
    # limit/SL orders for equity instruments are skipped by the order book,
    # and the catch-up reconciler ignores the equity daily-critical jobs.
    # F&O paths are unaffected. Default True keeps current behaviour.
    equity_trading_enabled: bool = Field(default=True, alias="EQUITY_TRADING_ENABLED")

    # --- Equity Strategy (Phase 2.5: LLM-driven paper trading) ---
    equity_strategy_enabled: bool = Field(default=False, alias="EQUITY_STRATEGY_ENABLED")
    # Portfolio scope: "lumpsum" reuses one persistent portfolio (cash carries
    # over, holdings carry over). "sip" tops up a fixed daily budget every
    # morning from idle cash; un-deployed cash rolls forward.
    equity_strategy_mode: str = Field(default="sip", alias="EQUITY_STRATEGY_MODE")
    # Daily budget for SIP mode — added to current_cash each morning.
    equity_strategy_daily_budget: float = Field(
        default=20000.0, alias="EQUITY_STRATEGY_DAILY_BUDGET"
    )
    # Lumpsum total capital — set once at portfolio bootstrap; topup is a no-op.
    equity_strategy_lumpsum_capital: float = Field(
        default=100000.0, alias="EQUITY_STRATEGY_LUMPSUM_CAPITAL"
    )
    # Per-position cap as a fraction of daily-budget (sip) or current_value (lumpsum).
    # Separated so each strategy mode has its own ceiling.
    equity_strategy_pos_cap_pct_sip: float = Field(
        default=0.40, alias="EQUITY_STRATEGY_POS_CAP_PCT_SIP"
    )
    equity_strategy_pos_cap_pct_lumpsum: float = Field(
        default=0.15, alias="EQUITY_STRATEGY_POS_CAP_PCT_LUMPSUM"
    )
    # Risk dial — "safe" tightens caps and prefers reserve cash; "balanced"
    # is default; "aggressive" allows fuller deployment & higher per-position caps.
    equity_strategy_risk_profile: str = Field(
        default="balanced", alias="EQUITY_STRATEGY_RISK_PROFILE"
    )
    # Models — Opus for high-stakes morning allocation, Sonnet for frequent intraday.
    equity_strategy_morning_model: str = Field(
        default="claude-opus-4-7", alias="EQUITY_STRATEGY_MORNING_MODEL"
    )
    equity_strategy_intraday_model: str = Field(
        default="claude-sonnet-4-6", alias="EQUITY_STRATEGY_INTRADAY_MODEL"
    )
    # Hard cap on intraday LLM calls per day to bound API cost.
    equity_strategy_max_intraday_calls: int = Field(
        default=8, alias="EQUITY_STRATEGY_MAX_INTRADAY_CALLS"
    )

    # --- Unified strategy budget (lumpsum, carry-forward) ---
    # Single common pool of paper capital shared across the equity LLM brain
    # and the four F&O strategy buckets. Capital carries forward day to day:
    # P&L flows through Portfolio.current_cash and rolls into tomorrow's pool.
    # The morning equity strategist (09:10 IST) picks today's per-bucket
    # allocation based on the market regime — the values below are the
    # fallback used when no allocation row exists for today (bootstrap, LLM
    # failure, equity strategy disabled).
    strategy_total_budget: float = Field(default=20000.0, alias="STRATEGY_TOTAL_BUDGET")
    strategy_default_alloc_equity: float = Field(
        default=0.50, alias="STRATEGY_DEFAULT_ALLOC_EQUITY"
    )
    strategy_default_alloc_fno_directional: float = Field(
        default=0.25, alias="STRATEGY_DEFAULT_ALLOC_FNO_DIRECTIONAL"
    )
    strategy_default_alloc_fno_spread: float = Field(
        default=0.15, alias="STRATEGY_DEFAULT_ALLOC_FNO_SPREAD"
    )
    strategy_default_alloc_fno_volatility: float = Field(
        default=0.10, alias="STRATEGY_DEFAULT_ALLOC_FNO_VOLATILITY"
    )

    # --- Dry-run replay ---
    dryrun_enabled: bool = Field(default=True, alias="DRYRUN_ENABLED")
    dryrun_historical_chain_source: str = Field(
        default="dhan", alias="DRYRUN_HISTORICAL_CHAIN_SOURCE"
    )
    dryrun_bhavcopy_cache_dir: str = Field(
        default="~/.cache/laabh/bhavcopy", alias="DRYRUN_BHAVCOPY_CACHE_DIR"
    )
    dryrun_dhan_cache_dir: str = Field(
        default="~/.cache/laabh/dhan_intraday", alias="DRYRUN_DHAN_CACHE_DIR"
    )
    dryrun_min_contract_oi: int = Field(default=1000, alias="DRYRUN_MIN_CONTRACT_OI")
    dryrun_min_contract_volume: int = Field(default=100, alias="DRYRUN_MIN_CONTRACT_VOLUME")
    dryrun_report_dir: str = Field(default="reports", alias="DRYRUN_REPORT_DIR")
    dryrun_llm_mode: str = Field(
        default="cached_or_live", alias="DRYRUN_LLM_MODE"
    )  # cached_or_live | mock | live

    # --- Quant (bandit-orchestrated intraday F&O) ---
    # LAABH_INTRADAY_MODE=quant bypasses all LLM intraday agents.
    # Default "agentic" keeps existing behaviour unchanged.
    laabh_intraday_mode: Literal["agentic", "quant"] = Field(
        default="agentic", alias="LAABH_INTRADAY_MODE"
    )
    laabh_quant_poll_interval_sec: int = Field(default=180, alias="LAABH_QUANT_POLL_INTERVAL_SEC")
    laabh_quant_primitives_enabled: str = Field(
        default="orb,vwap_revert,ofi,vol_breakout,momentum,index_revert",
        alias="LAABH_QUANT_PRIMITIVES_ENABLED",
    )
    laabh_quant_min_signal_strength: float = Field(
        default=0.4, alias="LAABH_QUANT_MIN_SIGNAL_STRENGTH"
    )
    laabh_quant_bandit_algo: Literal["thompson", "lints"] = Field(
        default="thompson", alias="LAABH_QUANT_BANDIT_ALGO"
    )
    laabh_quant_bandit_forget_factor: float = Field(
        default=0.95, alias="LAABH_QUANT_BANDIT_FORGET_FACTOR"
    )
    laabh_quant_bandit_prior_mean: float = Field(
        default=0.0, alias="LAABH_QUANT_BANDIT_PRIOR_MEAN"
    )
    laabh_quant_bandit_prior_var: float = Field(
        default=0.01, alias="LAABH_QUANT_BANDIT_PRIOR_VAR"
    )
    laabh_quant_bandit_seed: int | None = Field(
        default=None, alias="LAABH_QUANT_BANDIT_SEED"
    )
    laabh_quant_kelly_fraction: float = Field(
        default=0.5, alias="LAABH_QUANT_KELLY_FRACTION"
    )
    laabh_quant_max_per_trade_pct: float = Field(
        default=0.03, alias="LAABH_QUANT_MAX_PER_TRADE_PCT"
    )
    laabh_quant_max_total_exposure_pct: float = Field(
        default=0.30, alias="LAABH_QUANT_MAX_TOTAL_EXPOSURE_PCT"
    )
    laabh_quant_cost_gate_multiple: float = Field(
        default=3.0, alias="LAABH_QUANT_COST_GATE_MULTIPLE"
    )
    laabh_quant_lockin_target_pct: float = Field(
        default=0.05, alias="LAABH_QUANT_LOCKIN_TARGET_PCT"
    )
    laabh_quant_lockin_size_reduction: float = Field(
        default=0.5, alias="LAABH_QUANT_LOCKIN_SIZE_REDUCTION"
    )
    laabh_quant_kill_switch_dd_pct: float = Field(
        default=0.03, alias="LAABH_QUANT_KILL_SWITCH_DD_PCT"
    )
    laabh_quant_cooloff_consecutive_losses: int = Field(
        default=3, alias="LAABH_QUANT_COOLOFF_CONSECUTIVE_LOSSES"
    )
    laabh_quant_cooloff_minutes: int = Field(
        default=30, alias="LAABH_QUANT_COOLOFF_MINUTES"
    )
    laabh_quant_max_concurrent_positions: int = Field(
        default=8, alias="LAABH_QUANT_MAX_CONCURRENT_POSITIONS"
    )
    laabh_quant_universe_size_cap: int = Field(
        default=20, alias="LAABH_QUANT_UNIVERSE_SIZE_CAP"
    )
    laabh_quant_first_entry_after_minutes: int = Field(
        default=30, alias="LAABH_QUANT_FIRST_ENTRY_AFTER_MINUTES"
    )
    laabh_quant_hard_exit_time: time = Field(
        default=time(14, 30), alias="LAABH_QUANT_HARD_EXIT_TIME"
    )
    # Name of the portfolio to use for quant mode. Falls back to the first
    # active portfolio when unset (suitable for single-portfolio deployments).
    laabh_quant_portfolio_name: str = Field(
        default="", alias="LAABH_QUANT_PORTFOLIO_NAME"
    )

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic (replace asyncpg driver with psycopg2)."""
        return self.database_url.replace("+asyncpg", "+psycopg2")

    @property
    def quant_primitives_list(self) -> list[str]:
        """Return enabled primitives as a parsed list."""
        return [p.strip() for p in self.laabh_quant_primitives_enabled.split(",") if p.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
