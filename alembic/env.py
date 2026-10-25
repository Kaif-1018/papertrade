"""Alembic async migration environment."""

import asyncio
from logging.config import fileConfig
from pathlib import Path
import sys

# Ensure app package is importable when alembic CLI is invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Import models so autogenerate can diff the full schema.
#
# We must also ensure that every SQLAlchemy Enum type used in the ORM models
# carries create_type=False, so that importing the models here does NOT
# register DDL event listeners that auto-fire CREATE TYPE during migration
# runs.  Those enum types are created/dropped explicitly in each migration
# script via op.execute().
# ---------------------------------------------------------------------------
import app.models  # noqa: F401
from app.models.base import Base

target_metadata = Base.metadata

settings = get_settings()


def get_url() -> str:
    """Return the database URL from alembic config or settings."""
    return config.get_main_option("sqlalchemy.url") or settings.DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL without a live connection."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects via asyncpg."""
    connectable = create_async_engine(get_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
