"""Alembic: environment configuration.

The URL comes from .env via src.core.config, not from alembic.ini, so that
the secret does not sit in a version-controlled file (spec §15).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Models are imported to register them in the metadata: without it autogenerate
# will not see the tables and will generate an empty migration.
import src.core.db  # noqa: F401
from src.core.config import get_env
from src.core.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_env().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Schemas TimescaleDB keeps for its own needs (chunks, catalogs, caches).
# Their contents are not ours and must not end up in migrations.
_TIMESCALE_SCHEMAS = frozenset(
    {
        "_timescaledb_internal",
        "_timescaledb_catalog",
        "_timescaledb_config",
        "_timescaledb_cache",
        "_timescaledb_functions",
        "timescaledb_information",
        "timescaledb_experimental",
    }
)


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Filter for autogenerate.

    When a hypertable is created, Timescale itself creates an index on the
    time column (`<table>_ts_idx`). Our ORM metadata does not have it, so
    autogenerate considers it extra and proposes dropping it, and dropping
    it would break partitioning.

    We filter out such indexes and everything in the internal schemas.
    """
    if getattr(obj, "schema", None) in _TIMESCALE_SCHEMAS:
        return False
    timescale_time_index = (
        type_ == "index" and reflected and bool(name) and str(name).endswith("_ts_idx")
    )
    return not timescale_time_index


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
