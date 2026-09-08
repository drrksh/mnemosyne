"""Alembic migration environment for Mnemosyne.

This file deliberately reads the database URL from the application's
own `Settings` (env vars / .env) rather than duplicating it into
alembic.ini — there is exactly one place a connection string is
configured. A caller (test code, or a future ops script) can still
override it by calling `config.set_main_option("sqlalchemy.url", ...)`
on the Alembic Config object *before* invoking a command; this file
only falls back to Settings when nothing else has already provided a
URL.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure src/ is importable regardless of the working directory Alembic
# is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mnemosyne.config import get_settings  # noqa: E402
from mnemosyne.db.base import Base  # noqa: E402
from mnemosyne.repositories.sqlite import orm_models  # noqa: E402,F401  (registers models on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (emits SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most column/constraint types in place;
        # batch mode has Alembic rebuild the table (new table, copy,
        # swap) instead, which is the correct portable approach here
        # and is a no-op difference on backends (like PostgreSQL) that
        # support ALTER natively.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
