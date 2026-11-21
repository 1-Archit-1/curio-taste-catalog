"""Control-plane authentication: login token exchange and session verification."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from curio.crypto import generate_secret_token, hash_token
from curio.db.models.users import User
from curio.repositories import auth as auth_repo
from curio.repositories import users as users_repo


async def exchange_login_token(
    session: AsyncSession,
    *,
    login_token: str,
    ttl_days: int,
) -> tuple[User, str] | None:
    """Validate a login token and create a new session. Returns (user, raw_session_token)."""
    cleaned = login_token.strip()
    if not cleaned:
        return None

    user_id = await auth_repo.consume_login_token_lookup(
        session,
        token_hash=hash_token(cleaned),
    )
    if user_id is None:
        return None

    user = await users_repo.get_user_by_id(session, user_id=user_id)
    if user is None:
        return None

    # Nothing else removes expired rows, so prune this user's on each login.
    await auth_repo.delete_expired_sessions(session, user_id=user.id)
    session_token = generate_secret_token(32)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    await auth_repo.create_session(
        session,
        user_id=user.id,
        token_hash=hash_token(session_token),
        expires_at=expires_at,
    )
    return user, session_token


async def authenticate_session(
    session: AsyncSession,
    *,
    session_token: str | None,
) -> User | None:
    """Resolve an active session cookie token to its user, or None."""
    if not session_token:
        return None

    user_id = await auth_repo.find_user_id_by_session_hash(
        session,
        token_hash=hash_token(session_token),
    )
    if user_id is None:
        return None

    return await users_repo.get_user_by_id(session, user_id=user_id)


async def revoke_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_token: str,
) -> None:
    await auth_repo.delete_session(
        session,
        user_id=user_id,
        token_hash=hash_token(session_token),
    )
