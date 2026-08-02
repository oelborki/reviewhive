"""Alembic environment.

Async, because the application engine is asyncpg and running migrations through a
second sync driver would mean a second dependency and a second URL to keep in
step. `context.configure` and `context.run_migrations` are synchronous APIs, so
they run inside `connection.run_sync` rather than against the AsyncConnection.

The URL comes from `Settings`, never from alembic.ini. One source for the
database location means a stale value in a tracked file cannot quietly win over
the environment — `sqlalchemy.url` is left blank there on purpose.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from reviewhive.config import get_settings

# Imported for the side effect of registering every table on the metadata.
# Without it autogenerate compares against an empty model and cheerfully emits a
# migration that drops the schema.
from reviewhive.db import models  # noqa: F401
from reviewhive.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "REVIEWHIVE_DATABASE_URL is not set. Alembic reads the URL from "
            "Settings, not from alembic.ini. Start the database with "
            "`docker compose up -d db` and set the variable in .env."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # compare_type so a widened column or a changed numeric scale is noticed;
    # autogenerate ignores type changes by default.
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
