"""Control-plane authentication: login token exchange and session verification."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg

from curio.crypto import generate_secret_token, hash_token
from curio.repositories import auth as auth_repo
from curio.repositories import users as users_repo
from curio.repositories.users import UserRow


async def exchange_login_token(
    conn: psycopg.AsyncConnection,
    *,
    login_token: str,
    ttl_days: int,
) -> tuple[UserRow, str] | None:
    """Validate a login token and create a new session. Returns (user, raw_session_token)."""
    cleaned = login_token.strip()
    if not cleaned:
        return None

    user_id = await auth_repo.consume_login_token_lookup(
        conn,
        token_hash=hash_token(cleaned),
    )
    if user_id is None:
        return None

    user = await users_repo.get_user_by_id(conn, user_id=user_id)
    if user is None:
        return None

    session_token = generate_secret_token(32)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    await auth_repo.create_session(
        conn,
        user_id=user.id,
        token_hash=hash_token(session_token),
        expires_at=expires_at,
    )
    return user, session_token


async def authenticate_session(
    conn: psycopg.AsyncConnection,
    *,
    session_token: str | None,
) -> UserRow | None:
    """Resolve an active session cookie token to its user, or None."""
    if not session_token:
        return None

    user_id = await auth_repo.find_user_id_by_session_hash(
        conn,
        token_hash=hash_token(session_token),
    )
    if user_id is None:
        return None

    return await users_repo.get_user_by_id(conn, user_id=user_id)


async def revoke_session(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    session_token: str,
) -> None:
    await auth_repo.delete_session(
        conn,
        user_id=user_id,
        token_hash=hash_token(session_token),
    )
