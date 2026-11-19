"""Encrypted per-user API keys and decrypt audit log persistence."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class ApiKeyMetadataRow:
    user_id: UUID
    key_type: str
    last4: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EncryptedApiKeyRow:
    user_id: UUID
    key_type: str
    encrypted_key: bytes
    last4: str


@dataclass(frozen=True)
class AuditEntryRow:
    user_id: UUID
    id: int
    key_type: str
    job_id: str | None
    reason: str
    created_at: datetime


async def upsert_api_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: str,
    encrypted_key: bytes,
    last4: str,
) -> ApiKeyMetadataRow:
    async with conn.cursor(row_factory=class_row(ApiKeyMetadataRow)) as cur:
        await cur.execute(
            """
            INSERT INTO user_api_keys (user_id, key_type, encrypted_key, last4)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, key_type) DO UPDATE
            SET encrypted_key = EXCLUDED.encrypted_key,
                last4 = EXCLUDED.last4,
                updated_at = now()
            RETURNING user_id, key_type, last4, created_at, updated_at
            """,
            (user_id, key_type, encrypted_key, last4),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


async def list_api_key_metadata(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
) -> list[ApiKeyMetadataRow]:
    async with conn.cursor(row_factory=class_row(ApiKeyMetadataRow)) as cur:
        await cur.execute(
            """
            SELECT user_id, key_type, last4, created_at, updated_at
            FROM user_api_keys
            WHERE user_id = %s
            ORDER BY key_type
            """,
            (user_id,),
        )
        return list(await cur.fetchall())


async def get_encrypted_api_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: str,
) -> EncryptedApiKeyRow | None:
    async with conn.cursor(row_factory=class_row(EncryptedApiKeyRow)) as cur:
        await cur.execute(
            """
            SELECT user_id, key_type, encrypted_key, last4
            FROM user_api_keys
            WHERE user_id = %s
              AND key_type = %s
            """,
            (user_id, key_type),
        )
        return await cur.fetchone()


async def delete_api_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: str,
) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM user_api_keys
            WHERE user_id = %s
              AND key_type = %s
            """,
            (user_id, key_type),
        )
        return cur.rowcount > 0


async def insert_decrypt_audit(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: str,
    job_id: str | None,
    reason: str,
) -> AuditEntryRow:
    async with conn.cursor(row_factory=class_row(AuditEntryRow)) as cur:
        await cur.execute(
            """
            INSERT INTO key_decrypt_audit (user_id, key_type, job_id, reason)
            VALUES (%s, %s, %s, %s)
            RETURNING user_id, id, key_type, job_id, reason, created_at
            """,
            (user_id, key_type, job_id, reason),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


async def list_decrypt_audit(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    limit: int = 50,
) -> list[AuditEntryRow]:
    async with conn.cursor(row_factory=class_row(AuditEntryRow)) as cur:
        await cur.execute(
            """
            SELECT user_id, id, key_type, job_id, reason, created_at
            FROM key_decrypt_audit
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return list(await cur.fetchall())


async def lock_all_encrypted_keys(
    conn: psycopg.AsyncConnection,
) -> list[EncryptedApiKeyRow]:
    """Lock and return every stored encrypted key for keypair rotation."""
    async with conn.cursor(row_factory=class_row(EncryptedApiKeyRow)) as cur:
        await cur.execute(
            """
            SELECT user_id, key_type, encrypted_key, last4
            FROM user_api_keys
            ORDER BY user_id, key_type
            FOR UPDATE
            """
        )
        return list(await cur.fetchall())


async def update_encrypted_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: str,
    encrypted_key: bytes,
) -> None:
    await conn.execute(
        """
        UPDATE user_api_keys
        SET encrypted_key = %s,
            updated_at = now()
        WHERE user_id = %s
          AND key_type = %s
        """,
        (encrypted_key, user_id, key_type),
    )
