from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.db.models import Base

# Alembic Config object
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Build the SQLAlchemy URL from libpq-style DATABASE_DSN in app settings.
# Password stays as a plain value inside the URL object, so percent-encoding
# is not involved.
_settings = get_settings()
DATABASE_URL = _settings.database_url
DB_SCHEMA = _settings.db_schema

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        # Emit schema setup into the generated SQL so an offline script
        # (`alembic upgrade --sql`) targets DB_SCHEMA, not the default schema.
        # DB_SCHEMA is validated as a plain identifier in Settings.
        context.execute(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"')
        context.execute(f'SET search_path TO "{DB_SCHEMA}"')
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Ensure the dedicated schema exists before Alembic creates its version
    # table or any migration table inside it. Committed up front so it is in
    # place regardless of how the migration transaction unwinds.
    connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"')
    connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(
        DATABASE_URL,
        poolclass=pool.NullPool,
        # Pin to the dedicated schema so unqualified CREATE TABLE / index / the
        # alembic_version table all land inside it.
        connect_args={"server_settings": {"search_path": DB_SCHEMA}},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
