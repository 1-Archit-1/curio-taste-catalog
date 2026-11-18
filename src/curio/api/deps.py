from collections.abc import AsyncIterator

from fastapi import Request
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """One pooled connection per request. Commits on success, rolls back on error.

    Tests override this dependency to hand routes a connection inside a
    transaction that is rolled back after the test.
    """
    async with get_pool(request).connection() as conn:
        yield conn
