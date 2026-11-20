"""Login tokens and control-plane sessions persistence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.users import LoginToken, Session


async def create_login_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    token_hash: str,
) -> None:
    stmt = insert(LoginToken).values(user_id=user_id, token_hash=token_hash)
    await session.execute(stmt)


async def consume_login_token_lookup(
    session: AsyncSession,
    *,
    token_hash: str,
) -> UUID | None:
    """Resolve an active login token hash to its user_id and record last_used_at."""
    stmt = (
        update(LoginToken)
        .where(LoginToken.token_hash == token_hash, LoginToken.revoked_at.is_(None))
        .values(last_used_at=func.now())
        .returning(LoginToken.user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    token_hash: str,
    expires_at: datetime,
) -> None:
    stmt = insert(Session).values(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    await session.execute(stmt)


async def find_user_id_by_session_hash(
    session: AsyncSession,
    *,
    token_hash: str,
) -> UUID | None:
    stmt = select(Session.user_id).where(
        Session.token_hash == token_hash,
        Session.expires_at > func.now(),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def revoke_login_tokens(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> int:
    """Revoke all of the user's unrevoked login tokens. Returns how many were revoked."""
    stmt = (
        update(LoginToken)
        .where(LoginToken.user_id == user_id, LoginToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def delete_user_sessions(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> int:
    """Delete every session of the user. Returns how many were deleted."""
    stmt = delete(Session).where(Session.user_id == user_id)
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def delete_expired_sessions(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> int:
    """Delete the user's sessions that have expired. Returns how many were deleted."""
    stmt = delete(Session).where(
        Session.user_id == user_id,
        Session.expires_at <= func.now(),
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def delete_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    token_hash: str,
) -> None:
    stmt = delete(Session).where(
        Session.user_id == user_id,
        Session.token_hash == token_hash,
    )
    await session.execute(stmt)
