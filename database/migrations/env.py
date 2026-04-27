"""Alembic migration environment — uses sync psycopg2 driver and project Settings."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.config import get_settings
from src.db import Base
from src.models import register_all_models  # noqa: F401 — import to register metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject DB URL from Settings (sync form for Alembic).
# Escape `%` as `%%` so ConfigParser does not treat URL-encoded characters
# (e.g. `%40` for `@` in passwords) as interpolation syntax.
config.set_main_option(
    "sqlalchemy.url", get_settings().sync_database_url.replace("%", "%%")
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a DB connection (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
