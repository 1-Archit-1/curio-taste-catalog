"""Per-user API key management, worker key-access with audit logging, and rotation.

Follows ADR 0002:
- API processes encrypt with KEY_PUBLIC_KEY only; plaintext is tested, encrypted, and discarded.
- Workers decrypt just-in-time via `get_llm_key` / `get_tmdb_key` (backed by `get_user_api_key`),
  recording an audit row on every decryption.
"""

from dataclasses import dataclass
from uuid import UUID

import psycopg

from curio.clients.providers import (
    LLM_PROVIDERS,
    KeyType,
    KeyValidationResult,
    validate_provider_key,
)
from curio.crypto import KeyDecryptor, KeyEncryptor, mask_last4
from curio.repositories import keys as keys_repo
from curio.repositories import users as users_repo
from curio.repositories.keys import ApiKeyMetadataRow, AuditEntryRow


class KeyValidationFailedError(ValueError):
    """Raised when a submitted API key fails provider validation before storage."""


class KeyNotFoundError(LookupError):
    """Raised when a worker requests a key that the user has not configured."""


@dataclass(frozen=True)
class ResolvedLlmKey:
    provider: KeyType
    api_key: str


async def test_api_key(key_type: KeyType, api_key: str) -> KeyValidationResult:
    """Validate an API key against its provider without storing it."""
    return await validate_provider_key(key_type, api_key)


async def set_user_api_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: KeyType,
    api_key: str,
    public_key: str | None,
) -> ApiKeyMetadataRow:
    """Test an API key against the provider, then encrypt and store it."""
    encryptor = KeyEncryptor(public_key or "")

    validation = await validate_provider_key(key_type, api_key)
    if not validation.ok:
        raise KeyValidationFailedError(validation.detail)

    cleaned = api_key.strip()
    ciphertext = encryptor.encrypt(cleaned)
    last4 = mask_last4(cleaned)

    return await keys_repo.upsert_api_key(
        conn,
        user_id=user_id,
        key_type=key_type.value,
        encrypted_key=ciphertext,
        last4=last4,
    )


async def list_user_key_metadata(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
) -> dict[KeyType, ApiKeyMetadataRow]:
    rows = await keys_repo.list_api_key_metadata(conn, user_id=user_id)
    return {KeyType(row.key_type): row for row in rows}


async def delete_user_api_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: KeyType,
) -> bool:
    return await keys_repo.delete_api_key(conn, user_id=user_id, key_type=key_type.value)


async def get_user_api_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    key_type: KeyType,
    job_id: str | None,
    reason: str,
    private_key: str | None,
) -> str:
    """Single worker key-access function: decrypts just-in-time and writes an audit row."""
    decryptor = KeyDecryptor(private_key)
    row = await keys_repo.get_encrypted_api_key(
        conn,
        user_id=user_id,
        key_type=key_type.value,
    )
    if row is None:
        raise KeyNotFoundError(f"no {key_type.value} key configured for user {user_id}")

    plaintext = decryptor.decrypt(row.encrypted_key)
    await keys_repo.insert_decrypt_audit(
        conn,
        user_id=user_id,
        key_type=key_type.value,
        job_id=job_id,
        reason=reason,
    )
    return plaintext


async def get_tmdb_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    job_id: str | None,
    reason: str,
    private_key: str | None,
) -> str:
    """Decrypt the user's TMDB key for a worker job and log the access."""
    return await get_user_api_key(
        conn,
        user_id=user_id,
        key_type=KeyType.TMDB,
        job_id=job_id,
        reason=reason,
        private_key=private_key,
    )


async def get_llm_key(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    job_id: str | None,
    reason: str,
    private_key: str | None,
    provider: KeyType | None = None,
) -> ResolvedLlmKey:
    """Decrypt the user's active (or requested) LLM provider key for a worker job."""
    if provider is None:
        user = await users_repo.get_user_by_id(conn, user_id=user_id)
        if user is None:
            raise KeyNotFoundError(f"user {user_id} not found")
        provider = KeyType(user.llm_provider)
    elif provider not in LLM_PROVIDERS:
        raise ValueError(f"'{provider.value}' is not an LLM provider")

    plaintext = await get_user_api_key(
        conn,
        user_id=user_id,
        key_type=provider,
        job_id=job_id,
        reason=reason,
        private_key=private_key,
    )
    return ResolvedLlmKey(provider=provider, api_key=plaintext)


async def list_user_audit_entries(
    conn: psycopg.AsyncConnection,
    *,
    user_id: UUID,
    limit: int = 50,
) -> list[AuditEntryRow]:
    return await keys_repo.list_decrypt_audit(conn, user_id=user_id, limit=limit)


async def rotate_all_keys(
    conn: psycopg.AsyncConnection,
    *,
    old_private_key: str,
    new_public_key: str,
) -> int:
    """Re-encrypt every stored user API key under a new public key inside one transaction."""
    decryptor = KeyDecryptor(old_private_key)
    encryptor = KeyEncryptor(new_public_key)

    async with conn.transaction():
        rows = await keys_repo.lock_all_encrypted_keys(conn)
        for row in rows:
            plaintext = decryptor.decrypt(row.encrypted_key)
            new_ciphertext = encryptor.encrypt(plaintext)
            await keys_repo.update_encrypted_key(
                conn,
                user_id=row.user_id,
                key_type=row.key_type,
                encrypted_key=new_ciphertext,
            )
    return len(rows)
