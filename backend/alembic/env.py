"""Alembic environment (async). Uses DATABASE_URL from app settings."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().async_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


# Arbitrary constant: every migrator takes the same Postgres advisory lock, so
# when N backend pods start at once (each runs `alembic upgrade head` in an
# initContainer) they migrate one at a time and the rest see "already at head".
_MIGRATION_LOCK_ID = 7_263_510_001


def _do_run(connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(text(f"SELECT pg_advisory_lock({_MIGRATION_LOCK_ID})"))
        connection.commit()
    try:
        _migrate(connection)
    finally:
        if connection.dialect.name == "postgresql":
            connection.execute(text(f"SELECT pg_advisory_unlock({_MIGRATION_LOCK_ID})"))
            connection.commit()


def _migrate(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
