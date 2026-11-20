import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from curio.db.engine import to_sqlalchemy_url
from curio.db.models import Base
from curio.settings import Settings

# Arbitrary constant shared by every process running migrations.
ADVISORY_LOCK_ID = 7_265_310_001

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _configured_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url and not url.startswith("driver://"):
        return to_sqlalchemy_url(url)
    return to_sqlalchemy_url(Settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=_configured_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode under a session-level Postgres advisory lock."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _configured_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        await connection.commit()
        try:
            await connection.run_sync(do_run_migrations)
        finally:
            await connection.rollback()
            await connection.execute(
                text("SELECT pg_advisory_unlock(:id)"),
                {"id": ADVISORY_LOCK_ID},
            )
            await connection.commit()

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
