from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.db.models.users import User
from curio.services import auth as auth_service
from curio.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Commits on success, rolls back on error.

    Tests override this dependency to hand routes a session joined to an outer
    transaction that is rolled back after the test.
    """
    async with get_session_factory(request)() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


async def require_user(
    request: Request,
    session: DbSession,
    settings: AppSettings,
) -> User:
    """Authenticate a control-plane request via its HttpOnly session cookie."""
    session_token = request.cookies.get(settings.session_cookie_name)
    user = await auth_service.authenticate_session(session, session_token=session_token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return user


CurrentUser = Annotated[User, Depends(require_user)]
