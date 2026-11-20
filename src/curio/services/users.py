"""User provisioning, preferences, and addon-token management."""

from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.errors import UniqueViolation

from curio.clients.providers import LLM_PROVIDERS, KeyType
from curio.crypto import generate_secret_token, hash_token
from curio.repositories import auth as auth_repo
from curio.repositories import users as users_repo
from curio.repositories.users import UserRow


class UserAlreadyExistsError(ValueError):
    """Raised when creating a user with a username that is already taken."""


class UserNotFoundError(LookupError):
    """Raised when a target user does not exist."""


class InvalidProviderError(ValueError):
    """Raised when an invalid LLM provider is selected."""


@dataclass(frozen=True)
class ProvisionedUser:
    user: UserRow
    login_token: str


async def create_user_with_login_token(
    conn: psycopg.AsyncConnection,
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
        async with conn.transaction():
            user = await users_repo.create_user(
                conn,
                username=cleaned,
                addon_token=addon_token,
            )
            await auth_repo.create_login_token(
                conn,
                user_id=user.id,
                token_hash=token_hash,
            )
    except UniqueViolation as exc:
        raise UserAlreadyExistsError(f"user '{cleaned}' already exists") from exc

    return ProvisionedUser(user=user, login_token=login_token)


async def issue_login_token(
    conn: psycopg.AsyncConnection,
    *,
    username: str,
) -> tuple[UserRow, str]:
    user = await users_repo.get_user_by_username(conn, username=username.strip())
    if user is None:
        raise UserNotFoundError(f"user '{username}' not found")

    login_token = generate_secret_token(32)
    await auth_repo.create_login_token(
        conn,
        user_id=user.id,
        token_hash=hash_token(login_token),
    )
    return user, login_token


async def rotate_addon_token_for_username(
    conn: psycopg.AsyncConnection,
    *,
    username: str,
) -> UserRow:
    user = await users_repo.get_user_by_username(conn, username=username.strip())
    if user is None:
        raise UserNotFoundError(f"user '{username}' not found")
    return await rotate_addon_token_for_user(conn, user_id=user.id)


async def rotate_addon_token_for_user(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
) -> UserRow:
    new_token = generate_secret_token(24)
    updated = await users_repo.update_addon_token(
        conn,
        user_id=user_id,
        new_addon_token=new_token,
    )
    if updated is None:
        raise UserNotFoundError(f"user '{user_id}' not found")
    return updated


@dataclass(frozen=True)
class LoginTokenRevocation:
    user: UserRow
    login_tokens_revoked: int
    sessions_deleted: int


async def revoke_login_tokens_for_username(
    conn: psycopg.AsyncConnection,
    *,
    username: str,
) -> LoginTokenRevocation:
    user = await users_repo.get_user_by_username(conn, username=username.strip())
    if user is None:
        raise UserNotFoundError(f"user '{username}' not found")
    async with conn.transaction():
        tokens, sessions = await revoke_login_tokens_for_user(conn, user_id=user.id)
    return LoginTokenRevocation(user=user, login_tokens_revoked=tokens, sessions_deleted=sessions)


async def revoke_login_tokens_for_user(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
) -> tuple[int, int]:
    """Revoke the user's login tokens and delete their sessions. Returns (tokens, sessions)."""
    tokens = await auth_repo.revoke_login_tokens(conn, user_id=user_id)
    sessions = await auth_repo.delete_user_sessions(conn, user_id=user_id)
    return tokens, sessions


async def resolve_user_by_addon_token(
    conn: psycopg.AsyncConnection,
    *,
    addon_token: str,
) -> UserRow | None:
    if not addon_token:
        return None
    return await users_repo.get_user_by_addon_token(conn, addon_token=addon_token)


async def update_preferences(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    llm_provider: KeyType,
    monthly_token_budget: int | None,
) -> UserRow:
    if llm_provider not in LLM_PROVIDERS:
        raise InvalidProviderError(f"'{llm_provider.value}' is not an LLM provider")
    if monthly_token_budget is not None and monthly_token_budget <= 0:
        raise ValueError("monthly_token_budget must be a positive integer")

    updated = await users_repo.update_user_settings(
        conn,
        user_id=user_id,
        llm_provider=llm_provider.value,
        monthly_token_budget=monthly_token_budget,
    )
    if updated is None:
        raise UserNotFoundError(f"user '{user_id}' not found")
    return updated
