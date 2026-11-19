from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from curio.repositories.users import UserRow
from curio.services import auth as auth_service
from curio.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """One pooled connection per request. Commits on success, rolls back on error.

    Tests override this dependency to hand routes a connection inside a
    transaction that is rolled back after the test.
    """
    async with get_pool(request).connection() as conn:
        yield conn


DbConn = Annotated[AsyncConnection, Depends(get_conn)]
AppSettings = Annotated[Settings, Depends(get_settings)]


async def require_user(
    request: Request,
    conn: DbConn,
    settings: AppSettings,
) -> UserRow:
    """Authenticate a control-plane request via its HttpOnly session cookie."""
    session_token = request.cookies.get(settings.session_cookie_name)
    user = await auth_service.authenticate_session(conn, session_token=session_token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return user


CurrentUser = Annotated[UserRow, Depends(require_user)]
