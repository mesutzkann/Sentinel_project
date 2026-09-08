"""Alembic's entry point, wired to the project's settings and to ``rag.schema``.

Two things here are not the template's defaults, and both matter:

``version_table_schema`` puts ``alembic_version`` in ``rag`` rather than ``public``. The backend
runs EF Core migrations against the same database; two migration histories in one shared
namespace is a collision waiting for the day someone runs both against a fresh volume.

``include_object`` keeps autogenerate from noticing the backend's tables. Without it, the first
``--autogenerate`` after the backend has migrated proposes dropping every table in ``sentinel``.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# The migrations directory is not on the path when alembic runs from ai-service/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from rag.schema import SCHEMA, metadata  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings().database_url)

target_metadata = metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001, ARG001
    """Only the ``rag`` schema is this history's business."""
    if type_ == "table":
        return obj.schema == SCHEMA

    return True


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_object=include_object,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — ``alembic upgrade head --sql``."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with engine.connect() as connection:
        await connection.run_sync(_run)

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
