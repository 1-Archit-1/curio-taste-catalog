"""Integration fixtures against real Postgres.

Uses TEST_DATABASE_URL if set (CI, or a local Postgres you started); otherwise
starts a throwaway pgvector container via testcontainers. Migrations run once
per session. Each test gets an AsyncSession joined to an outer transaction that is
rolled back afterwards, so tests never see each other's writes. Tests that exercise
code writing on a second session (the decrypt audit) use `audit_session_factory` and
`make_committed_user`, because rows created inside the rolled-back outer transaction
are invisible to other connections.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from curio.api.app import create_app
from curio.api.deps import get_session
from curio.crypto import Keypair, generate_keypair
from curio.db.engine import to_sqlalchemy_url
from curio.db.models.users import User
from curio.services import users as users_service
from curio.services.users import ProvisionedUser
from curio.settings import Settings

PGVECTOR_IMAGE = "pgvector/pgvector:pg16"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "src" / "curio" / "db" / "alembic.ini"


def make_alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", to_sqlalchemy_url(database_url))
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(PGVECTOR_IMAGE, driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    command.upgrade(make_alembic_config(database_url), "head")
    return database_url


@pytest.fixture
async def session(migrated_database_url: str) -> AsyncIterator[AsyncSession]:
    """Session joined to an outer transaction that is rolled back after the test."""
    engine = create_async_engine(to_sqlalchemy_url(migrated_database_url), poolclass=NullPool)
    async with engine.connect() as conn:
        trans = await conn.begin()
        async_session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            if trans.is_active:
                await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def audit_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory for separate-session decrypt audit writes (what a worker passes in)."""
    engine = create_async_engine(to_sqlalchemy_url(migrated_database_url), poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def make_committed_user(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Callable[[str], Awaitable[ProvisionedUser]]]:
    """Create users that are committed, so other sessions can reference them.

    On teardown, rolls back `session` (releasing its locks) and deletes the users; the delete
    cascades to their tokens, sessions, keys, and audit rows.
    """
    usernames: list[str] = []

    async def make(username: str) -> ProvisionedUser:
        async with audit_session_factory() as setup_session:
            provisioned = await users_service.create_user_with_login_token(
                setup_session, username=username
            )
            await setup_session.commit()
        usernames.append(username)
        return provisioned

    yield make

    await session.rollback()
    if usernames:
        async with audit_session_factory() as cleanup_session:
            await cleanup_session.execute(delete(User).where(User.username.in_(usernames)))
            await cleanup_session.commit()


@pytest.fixture(scope="session")
def keypair() -> Keypair:
    return generate_keypair()


@pytest.fixture
async def client(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    keypair: Keypair,
) -> AsyncIterator[httpx.AsyncClient]:
    """API client whose routes share the test's rolled-back session.

    Configured with KEY_PUBLIC_KEY only (no KEY_PRIVATE_KEY), matching ADR 0002.
    The app lifespan is not run, so no engine/pool is opened.
    """
    app = create_app(
        Settings(
            _env_file=None,
            database_url=migrated_database_url,
            key_public_key=keypair.public_key,
            key_private_key=None,
        )
    )
    app.state.session_factory = audit_session_factory

    async def test_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
