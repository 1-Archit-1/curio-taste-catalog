"""User provisioning, preferences, and addon-token management."""

from dataclasses import dataclass
from uuid import UUID

import psycopg.errors
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from curio.clients.providers import LLM_PROVIDERS, KeyType
from curio.crypto import generate_secret_token, hash_token
from curio.db.models.users import User
from curio.repositories import auth as auth_repo
from curio.repositories import users as users_repo


class UserAlreadyExistsError(ValueError):
    """Raised when creating a user with a username that is already taken."""


class UserNotFoundError(LookupError):
    """Raised when a target user does not exist."""


class InvalidProviderError(ValueError):
    """Raised when an invalid LLM provider is selected."""


@dataclass(frozen=True)
class ProvisionedUser:
    user: User
    login_token: str


async def create_user_with_login_token(
    session: AsyncSession,
    *,
    username: str,
) -> ProvisionedUser:
    cleaned = username.strip()
    if not cleaned:
        raise ValueError("username cannot be empty")

    addon_token = generate_secret_token(24)
    login_token = generate_secret_token(32)
    token_hash = hash_token(login_token)

    try:
        async with session.begin_nested():
            user = await users_repo.create_user(
                session,
                username=cleaned,
                addon_token=addon_token,
            )
            await auth_repo.create_login_token(
                session,
                user_id=user.id,
                token_hash=token_hash,
            )
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            raise UserAlreadyExistsError(f"user '{cleaned}' already exists") from exc
        raise

    return ProvisionedUser(user=user, login_token=login_token)


async def issue_login_token(
    session: AsyncSession,
    *,
    username: str,
) -> tuple[User, str]:
    user = await users_repo.get_user_by_username(session, username=username.strip())
    if user is None:
        raise UserNotFoundError(f"user '{username}' not found")

    login_token = generate_secret_token(32)
    await auth_repo.create_login_token(
        session,
        user_id=user.id,
        token_hash=hash_token(login_token),
    )
    return user, login_token


async def rotate_addon_token_for_username(
    session: AsyncSession,
    *,
    username: str,
) -> User:
    user = await users_repo.get_user_by_username(session, username=username.strip())
    if user is None:
        raise UserNotFoundError(f"user '{username}' not found")
    return await rotate_addon_token_for_user(session, user_id=user.id)


async def rotate_addon_token_for_user(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> User:
    new_token = generate_secret_token(24)
    updated = await users_repo.update_addon_token(
        session,
        user_id=user_id,
        new_addon_token=new_token,
    )
    if updated is None:
        raise UserNotFoundError(f"user '{user_id}' not found")
    return updated


@dataclass(frozen=True)
class LoginTokenRevocation:
    user: User
    login_tokens_revoked: int
    sessions_deleted: int


async def revoke_login_tokens_for_username(
    session: AsyncSession,
    *,
    username: str,
) -> LoginTokenRevocation:
    user = await users_repo.get_user_by_username(session, username=username.strip())
    if user is None:
        raise UserNotFoundError(f"user '{username}' not found")
    async with session.begin_nested():
        tokens, sessions = await revoke_login_tokens_for_user(session, user_id=user.id)
    return LoginTokenRevocation(user=user, login_tokens_revoked=tokens, sessions_deleted=sessions)


async def revoke_login_tokens_for_user(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> tuple[int, int]:
    """Revoke the user's login tokens and delete their sessions. Returns (tokens, sessions)."""
    tokens = await auth_repo.revoke_login_tokens(session, user_id=user_id)
    sessions = await auth_repo.delete_user_sessions(session, user_id=user_id)
    return tokens, sessions


async def resolve_user_by_addon_token(
    session: AsyncSession,
    *,
    addon_token: str,
) -> User | None:
    if not addon_token:
        return None
    return await users_repo.get_user_by_addon_token(session, addon_token=addon_token)


async def update_preferences(
    session: AsyncSession,
    *,
    user_id: UUID,
    llm_provider: KeyType,
    monthly_token_budget: int | None,
) -> User:
    if llm_provider not in LLM_PROVIDERS:
        raise InvalidProviderError(f"'{llm_provider.value}' is not an LLM provider")
    if monthly_token_budget is not None and monthly_token_budget <= 0:
        raise ValueError("monthly_token_budget must be a positive integer")

    updated = await users_repo.update_user_settings(
        session,
        user_id=user_id,
        llm_provider=llm_provider.value,
        monthly_token_budget=monthly_token_budget,
    )
    if updated is None:
        raise UserNotFoundError(f"user '{user_id}' not found")
    return updated
