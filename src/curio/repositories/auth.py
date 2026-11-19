"""Login tokens and control-plane sessions persistence."""

from datetime import datetime
from uuid import UUID

import psycopg


async def create_login_token(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    token_hash: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO login_tokens (user_id, token_hash)
        VALUES (%s, %s)
        """,
        (user_id, token_hash),
    )


async def consume_login_token_lookup(
    conn: psycopg.AsyncConnection,
    *,
    token_hash: str,
) -> UUID | None:
    """Resolve an active login token hash to its user_id and record last_used_at."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE login_tokens
            SET last_used_at = now()
            WHERE token_hash = %s
              AND revoked_at IS NULL
            RETURNING user_id
            """,
            (token_hash,),
        )
        row = await cur.fetchone()
    return row[0] if row is not None else None


async def create_session(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    token_hash: str,
    expires_at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO sessions (user_id, token_hash, expires_at)
        VALUES (%s, %s, %s)
        """,
        (user_id, token_hash, expires_at),
    )


async def find_user_id_by_session_hash(
    conn: psycopg.AsyncConnection,
    *,
    token_hash: str,
) -> UUID | None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user_id
            FROM sessions
            WHERE token_hash = %s
              AND expires_at > now()
            """,
            (token_hash,),
        )
        row = await cur.fetchone()
    return row[0] if row is not None else None


async def delete_session(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    token_hash: str,
) -> None:
    await conn.execute(
        """
        DELETE FROM sessions
        WHERE user_id = %s
          AND token_hash = %s
        """,
        (user_id, token_hash),
    )
