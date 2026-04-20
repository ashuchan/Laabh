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
        default="claude-sonnet-4-20250514", alias="ANTHROPIC_MODEL"
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # --- General ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="Asia/Kolkata", alias="TIMEZONE")
    market_open_time: str = Field(default="09:15", alias="MARKET_OPEN_TIME")
    market_close_time: str = Field(default="15:30", alias="MARKET_CLOSE_TIME")

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic (replace asyncpg driver with psycopg2)."""
        return self.database_url.replace("+asyncpg", "+psycopg2")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
