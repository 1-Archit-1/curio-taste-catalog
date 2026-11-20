"""Async SQLAlchemy engine and session factory builders (no module-level DB state)."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from curio.settings import Settings


def to_sqlalchemy_url(database_url: str) -> str:
    """Normalize a Postgres URL to use the psycopg 3 async dialect."""
    if database_url.startswith("postgresql+"):
        return database_url
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    return database_url


def create_engine(settings: Settings, *, echo: bool = False) -> AsyncEngine:
    """Build an AsyncEngine. The caller disposes it on shutdown."""
    return create_async_engine(
        to_sqlalchemy_url(settings.database_url),
        pool_size=settings.db_pool_max_size,
        max_overflow=0,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        echo=echo,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an async_sessionmaker with expire_on_commit=False (ADR 0005)."""
    return async_sessionmaker(engine, expire_on_commit=False)
