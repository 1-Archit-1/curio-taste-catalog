"""Encrypted per-user API keys and decrypt audit log persistence."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.keys import KeyDecryptAudit, UserApiKey


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


async def upsert_api_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
    encrypted_key: bytes,
    last4: str,
) -> ApiKeyMetadataRow:
    insert_stmt = postgresql.insert(UserApiKey).values(
        user_id=user_id,
        key_type=key_type,
        encrypted_key=encrypted_key,
        last4=last4,
    )
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[UserApiKey.user_id, UserApiKey.key_type],
        set_={
            "encrypted_key": insert_stmt.excluded.encrypted_key,
            "last4": insert_stmt.excluded.last4,
            "updated_at": func.now(),
        },
    ).returning(
        UserApiKey.user_id,
        UserApiKey.key_type,
        UserApiKey.last4,
        UserApiKey.created_at,
        UserApiKey.updated_at,
    )
    row = (await session.execute(stmt)).one()
    return ApiKeyMetadataRow(
        user_id=row.user_id,
        key_type=row.key_type,
        last4=row.last4,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_api_key_metadata(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[ApiKeyMetadataRow]:
    # Display query: never select encrypted_key.
    stmt = (
        select(
            UserApiKey.user_id,
            UserApiKey.key_type,
            UserApiKey.last4,
            UserApiKey.created_at,
            UserApiKey.updated_at,
        )
        .where(UserApiKey.user_id == user_id)
        .order_by(UserApiKey.key_type)
    )
    rows = (await session.execute(stmt)).all()
    return [
        ApiKeyMetadataRow(
            user_id=row.user_id,
            key_type=row.key_type,
            last4=row.last4,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def get_encrypted_api_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
) -> EncryptedApiKeyRow | None:
    stmt = select(
        UserApiKey.user_id,
        UserApiKey.key_type,
        UserApiKey.encrypted_key,
        UserApiKey.last4,
    ).where(
        UserApiKey.user_id == user_id,
        UserApiKey.key_type == key_type,
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return EncryptedApiKeyRow(
        user_id=row.user_id,
        key_type=row.key_type,
        encrypted_key=row.encrypted_key,
        last4=row.last4,
    )


async def delete_api_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
) -> bool:
    stmt = delete(UserApiKey).where(
        UserApiKey.user_id == user_id,
        UserApiKey.key_type == key_type,
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0) > 0


async def insert_decrypt_audit(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
    job_id: str | None,
    reason: str,
) -> KeyDecryptAudit:
    stmt = (
        insert(KeyDecryptAudit)
        .values(user_id=user_id, key_type=key_type, job_id=job_id, reason=reason)
        .returning(KeyDecryptAudit)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_decrypt_audit(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int = 50,
) -> list[KeyDecryptAudit]:
    stmt = (
        select(KeyDecryptAudit)
        .where(KeyDecryptAudit.user_id == user_id)
        .order_by(KeyDecryptAudit.created_at.desc(), KeyDecryptAudit.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def lock_all_encrypted_keys(
    session: AsyncSession,
) -> list[EncryptedApiKeyRow]:
    """Lock and return every stored encrypted key for keypair rotation."""
    stmt = (
        select(
            UserApiKey.user_id,
            UserApiKey.key_type,
            UserApiKey.encrypted_key,
            UserApiKey.last4,
        )
        .order_by(UserApiKey.user_id, UserApiKey.key_type)
        .with_for_update()
    )
    rows = (await session.execute(stmt)).all()
    return [
        EncryptedApiKeyRow(
            user_id=row.user_id,
            key_type=row.key_type,
            encrypted_key=row.encrypted_key,
            last4=row.last4,
        )
        for row in rows
    ]


async def update_encrypted_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
    encrypted_key: bytes,
) -> None:
    stmt = (
        update(UserApiKey)
        .where(UserApiKey.user_id == user_id, UserApiKey.key_type == key_type)
        .values(encrypted_key=encrypted_key, updated_at=func.now())
    )
    await session.execute(stmt)
