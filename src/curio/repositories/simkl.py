"""Database persistence for Simkl OAuth states, connections, and pending revocations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.simkl import SimklConnection, SimklOAuthState, SimklRevocation

# --- OAuth states -----------------------------------------------------------------------------


async def create_oauth_state(
    session: AsyncSession,
    *,
    state_hash: str,
    user_id: UUID,
    session_hash: str,
    code_verifier: str,
    expires_at: datetime,
) -> None:
    session.add(
        SimklOAuthState(
            state_hash=state_hash,
            user_id=user_id,
            session_hash=session_hash,
            code_verifier=code_verifier,
            expires_at=expires_at,
        )
    )
    await session.flush()


async def consume_oauth_state(session: AsyncSession, *, state_hash: str) -> SimklOAuthState | None:
    """Delete and return the state, in one statement, so it can only be used once.

    The caller checks the user, the session and the expiry on the returned row. The row is gone
    whatever those checks say.
    """
    stmt = (
        delete(SimklOAuthState)
        .where(SimklOAuthState.state_hash == state_hash)
        .returning(SimklOAuthState)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def delete_expired_oauth_states(session: AsyncSession) -> None:
    await session.execute(delete(SimklOAuthState).where(SimklOAuthState.expires_at <= func.now()))


# --- Connections ------------------------------------------------------------------------------


async def get_connection(
    session: AsyncSession, *, user_id: UUID, for_update: bool = False
) -> SimklConnection | None:
    stmt = select(SimklConnection).where(SimklConnection.user_id == user_id)
    if for_update:
        # A grant supports one live access token, so only one process may refresh it at a time.
        stmt = stmt.with_for_update()
    return await session.scalar(stmt.execution_options(populate_existing=True))


async def upsert_connection(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
    access_token_ciphertext: bytes,
    refresh_token_ciphertext: bytes,
    access_expires_at: datetime,
    scope: str,
    simkl_account_id: int | None,
    simkl_account_name: str | None,
) -> SimklConnection:
    """Store a fresh grant. A new grant starts over: no snapshot (the next sync is a full pull
    of whichever account was connected), no error and no reconnect flag.
    """
    values: dict[str, Any] = {
        "source_id": source_id,
        "access_token_ciphertext": access_token_ciphertext,
        "refresh_token_ciphertext": refresh_token_ciphertext,
        "access_expires_at": access_expires_at,
        "scope": scope,
        "simkl_account_id": simkl_account_id,
        "simkl_account_name": simkl_account_name,
        "needs_reconnect": False,
        "last_error": None,
        "activities_snapshot": None,
    }
    stmt = pg_insert(SimklConnection).values(user_id=user_id, **values)
    stmt = (
        stmt.on_conflict_do_update(
            index_elements=["user_id"], set_={**values, "updated_at": func.now()}
        )
        .returning(SimklConnection)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def update_connection_tokens(
    session: AsyncSession,
    *,
    user_id: UUID,
    access_token_ciphertext: bytes,
    refresh_token_ciphertext: bytes,
    access_expires_at: datetime,
    scope: str,
) -> None:
    await session.execute(
        update(SimklConnection)
        .where(SimklConnection.user_id == user_id)
        .values(
            access_token_ciphertext=access_token_ciphertext,
            refresh_token_ciphertext=refresh_token_ciphertext,
            access_expires_at=access_expires_at,
            scope=scope,
            updated_at=func.now(),
        )
    )


async def record_sync_success(
    session: AsyncSession,
    *,
    user_id: UUID,
    snapshot: dict[str, Any],
) -> None:
    await session.execute(
        update(SimklConnection)
        .where(SimklConnection.user_id == user_id)
        .values(
            activities_snapshot=snapshot,
            last_synced_at=func.now(),
            last_error=None,
            needs_reconnect=False,
            updated_at=func.now(),
        )
    )


async def record_sync_failure(
    session: AsyncSession,
    *,
    user_id: UUID,
    error: str,
    needs_reconnect: bool,
) -> None:
    """Store the error, and set `needs_reconnect` (never clear it: only a new grant does)."""
    values: dict[str, Any] = {"last_error": error, "updated_at": func.now()}
    if needs_reconnect:
        values["needs_reconnect"] = True
    await session.execute(
        update(SimklConnection).where(SimklConnection.user_id == user_id).values(**values)
    )


async def delete_connection(session: AsyncSession, *, user_id: UUID) -> None:
    await session.execute(delete(SimklConnection).where(SimklConnection.user_id == user_id))


async def lock_all_connections(session: AsyncSession) -> list[SimklConnection]:
    """Lock every connection for keypair rotation."""
    rows = await session.scalars(
        select(SimklConnection)
        .order_by(SimklConnection.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(rows)


# --- Revocations ------------------------------------------------------------------------------


async def create_revocation(
    session: AsyncSession, *, user_id: UUID, refresh_token_ciphertext: bytes
) -> SimklRevocation:
    revocation = SimklRevocation(user_id=user_id, refresh_token_ciphertext=refresh_token_ciphertext)
    session.add(revocation)
    await session.flush()
    return revocation


async def get_revocation(
    session: AsyncSession, *, user_id: UUID, revocation_id: UUID
) -> SimklRevocation | None:
    return await session.scalar(
        select(SimklRevocation).where(
            SimklRevocation.user_id == user_id, SimklRevocation.id == revocation_id
        )
    )


async def delete_revocation(session: AsyncSession, *, user_id: UUID, revocation_id: UUID) -> None:
    await session.execute(
        delete(SimklRevocation).where(
            SimklRevocation.user_id == user_id, SimklRevocation.id == revocation_id
        )
    )


async def lock_all_revocations(session: AsyncSession) -> list[SimklRevocation]:
    rows = await session.scalars(
        select(SimklRevocation)
        .order_by(SimklRevocation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(rows)
