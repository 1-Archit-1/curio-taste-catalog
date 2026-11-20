"""User persistence. Only SQL lives here; imports psycopg only."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import class_row

_USER_COLUMNS = (
    "id, username, addon_token, llm_provider, monthly_token_budget, "
    "jobs_paused_reason, created_at, updated_at"
)


@dataclass(frozen=True)
class UserRow:
    id: UUID
    username: str
    addon_token: str
    llm_provider: str
    monthly_token_budget: int | None
    jobs_paused_reason: str | None
    created_at: datetime
    updated_at: datetime


async def create_user(
    conn: psycopg.AsyncConnection,
    *,
    username: str,
    addon_token: str,
) -> UserRow:
    async with conn.cursor(row_factory=class_row(UserRow)) as cur:
        await cur.execute(
            f"""
            INSERT INTO users (username, addon_token)
            VALUES (%s, %s)
            RETURNING {_USER_COLUMNS}
            """,
            (username, addon_token),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


async def get_user_by_id(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
) -> UserRow | None:
    async with conn.cursor(row_factory=class_row(UserRow)) as cur:
        await cur.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s",
            (user_id,),
        )
        return await cur.fetchone()


async def get_user_by_username(
    conn: psycopg.AsyncConnection,
    *,
    username: str,
) -> UserRow | None:
    async with conn.cursor(row_factory=class_row(UserRow)) as cur:
        await cur.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE username = %s",
            (username,),
        )
        return await cur.fetchone()


async def get_user_by_addon_token(
    conn: psycopg.AsyncConnection,
    *,
    addon_token: str,
) -> UserRow | None:
    async with conn.cursor(row_factory=class_row(UserRow)) as cur:
        await cur.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE addon_token = %s",
            (addon_token,),
        )
        return await cur.fetchone()


async def update_addon_token(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    new_addon_token: str,
) -> UserRow | None:
    async with conn.cursor(row_factory=class_row(UserRow)) as cur:
        await cur.execute(
            f"""
            UPDATE users
            SET addon_token = %s, updated_at = now()
            WHERE id = %s
            RETURNING {_USER_COLUMNS}
            """,
            (new_addon_token, user_id),
        )
        return await cur.fetchone()


async def update_user_settings(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    llm_provider: str,
    monthly_token_budget: int | None,
) -> UserRow | None:
    async with conn.cursor(row_factory=class_row(UserRow)) as cur:
        await cur.execute(
            f"""
            UPDATE users
            SET llm_provider = %s,
                monthly_token_budget = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING {_USER_COLUMNS}
            """,
            (llm_provider, monthly_token_budget, user_id),
        )
        return await cur.fetchone()
