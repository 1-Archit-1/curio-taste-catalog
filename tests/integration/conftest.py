"""Integration fixtures against real Postgres.

Uses TEST_DATABASE_URL if set (CI, or a local Postgres you started); otherwise
starts a throwaway pgvector container via testcontainers. Migrations run once
per session. Each test gets a connection inside a transaction that is rolled
back afterwards, so tests never see each other's writes.
"""

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import psycopg
import pytest

from curio.api.app import create_app
from curio.api.deps import get_conn
from curio.db.migrate import migrate
from curio.settings import Settings

PGVECTOR_IMAGE = "pgvector/pgvector:pg16"


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
    migrate(database_url)
    return database_url


@pytest.fixture
async def conn(migrated_database_url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(migrated_database_url)
    try:
        yield conn
    finally:
        await conn.rollback()
        await conn.close()


@pytest.fixture
async def client(
    conn: psycopg.AsyncConnection, migrated_database_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    """API client whose routes share the test's rolled-back connection.

    The app lifespan is not run, so no pool is opened.
    """
    app = create_app(Settings(_env_file=None, database_url=migrated_database_url))

    async def test_conn() -> AsyncIterator[psycopg.AsyncConnection]:
        yield conn

    app.dependency_overrides[get_conn] = test_conn
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
