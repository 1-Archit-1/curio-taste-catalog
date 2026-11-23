"""User persistence using SQLAlchemy 2.0."""

from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.users import User


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    addon_token: str,
) -> User:
    stmt = insert(User).values(username=username, addon_token=addon_token).returning(User)
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_user_by_id(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> User | None:
    stmt = select(User).where(User.id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_user_by_username(
    session: AsyncSession,
    *,
    username: str,
) -> User | None:
    stmt = select(User).where(User.username == username)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_user_by_addon_token(
    session: AsyncSession,
    *,
    addon_token: str,
) -> User | None:
    stmt = select(User).where(User.addon_token == addon_token)
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_addon_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    new_addon_token: str,
) -> User | None:
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(addon_token=new_addon_token, updated_at=func.now())
        .returning(User)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_user_settings(
    session: AsyncSession,
    *,
    user_id: UUID,
    llm_provider: str,
    monthly_token_budget: int | None,
) -> User | None:
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(
            llm_provider=llm_provider,
            monthly_token_budget=monthly_token_budget,
            updated_at=func.now(),
        )
        .returning(User)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_jobs_paused_reason(
    session: AsyncSession,
    *,
    user_id: UUID,
    reason: str | None,
) -> None:
    """Update `users.jobs_paused_reason` for `user_id`."""
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(jobs_paused_reason=reason, updated_at=func.now())
    )
    await session.execute(stmt)
