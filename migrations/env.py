"""Alembic environment.

The database URL is *not* read from `alembic.ini`. It comes from the same `Settings` the
application uses, so a DSN is configured in exactly one place and there is no chance of
migrating a different database than the one the service talks to.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from taskflow_api.config import get_settings

# Importing the models registers every table on Base.metadata. Without this import,
# autogenerate sees an empty schema and cheerfully writes a migration that drops
# everything.
from taskflow_api.db import models  # noqa: F401  (imported for the side effect)
from taskflow_api.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Respect a URL the caller already set (the integration fixture points this at a
# throwaway container). Falling back to Settings keeps a single source of truth for
# ordinary runs, without the fixture having to mutate the process environment — which
# would leak into every other test in the session.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it, for review or manual application."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an established connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without these two, autogenerate ignores a changed column type or a changed
        # server default, and the migrations silently drift from the models.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run the migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
