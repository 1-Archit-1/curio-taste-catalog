"""Simkl connection: the OAuth code flow, connection status, manual sync, and disconnect (#6).

This is the API side. The API holds only the public key (ADR 0002), so it can seal the tokens it
receives but never read them back. Everything that needs a token runs in a worker job; see
`services.simkl_sync`.

The flow is AUTH V2, authorization code with PKCE (S256), a `state` bound to the user and the
browser session, and an `iss` check (https://api.simkl.org/api-reference/oauth2-authorization-code).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from curio.clients.providers import KeyType
from curio.clients.simkl import (
    SIMKL_ISSUER,
    SimklClient,
    SimklError,
    SimklTokens,
    build_authorize_url,
    generate_pkce,
)
from curio.crypto import KeyEncryptor, PublicKeyNotConfiguredError, hash_token
from curio.db.models.jobs import Job
from curio.db.models.simkl import SimklConnection
from curio.repositories import jobs as jobs_repo
from curio.repositories import simkl as simkl_repo
from curio.repositories import sources as sources_repo
from curio.services import jobs as jobs_service
from curio.services import keys as keys_service
from curio.services import sources as sources_service
from curio.settings import Settings

log = logging.getLogger(__name__)

OAUTH_STATE_TTL = timedelta(minutes=10)
SIMKL_SOURCE_KEY = "account"
SIMKL_SOURCE_ROLE = "self"

CallbackOutcome = Literal["connected", "cancelled", "error"]


class SimklNotConfiguredError(Exception):
    """The operator has not set the Simkl client ID, secret and redirect URI."""


class SimklNotConnectedError(LookupError):
    """The user has no Simkl connection."""


class SimklReconnectRequiredError(Exception):
    """Simkl rejected the stored grant, so nothing can sync until the user connects again."""


def _require_configured(settings: Settings) -> tuple[str, str, str]:
    """The client ID, secret and redirect URI, or `SimklNotConfiguredError`."""
    redirect_uri = settings.simkl_effective_redirect_uri
    if (
        not settings.simkl_oauth_configured
        or settings.simkl_client_id is None
        or settings.simkl_client_secret is None
        or redirect_uri is None
    ):
        raise SimklNotConfiguredError(
            "Simkl isn't set up on this server. The operator needs to set SIMKL_CLIENT_ID, "
            "SIMKL_CLIENT_SECRET and PUBLIC_BASE_URL (or SIMKL_REDIRECT_URI)."
        )
    return settings.simkl_client_id, settings.simkl_client_secret.get_secret_value(), redirect_uri


def _make_client(settings: Settings) -> SimklClient:
    client_id, client_secret, _ = _require_configured(settings)
    return SimklClient(client_id, client_secret)


@dataclass(frozen=True)
class SimklStatus:
    configured: bool
    connection: SimklConnection | None
    sync_job: Job | None


async def get_status(session: AsyncSession, *, user_id: UUID, settings: Settings) -> SimklStatus:
    return SimklStatus(
        configured=settings.simkl_oauth_configured,
        connection=await simkl_repo.get_connection(session, user_id=user_id),
        sync_job=await jobs_repo.get_running_job(session, user_id=user_id, job_kind="simkl_sync"),
    )


# --- Connect and callback ---------------------------------------------------------------------


async def start_connect(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_token: str,
    settings: Settings,
) -> str:
    """Store a fresh `state` and PKCE verifier and return the Simkl consent URL.

    The `redirect_uri` comes from server settings, never from the request.
    """
    client_id, _, redirect_uri = _require_configured(settings)
    if not settings.key_public_key:
        # Without it the callback couldn't seal the tokens, and the consent step would be wasted.
        raise PublicKeyNotConfiguredError("KEY_PUBLIC_KEY is not configured")
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce()
    await simkl_repo.delete_expired_oauth_states(session)
    await simkl_repo.create_oauth_state(
        session,
        state_hash=hash_token(state),
        user_id=user_id,
        session_hash=hash_token(session_token),
        code_verifier=verifier,
        expires_at=datetime.now(UTC) + OAUTH_STATE_TTL,
    )
    return build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
    )


async def handle_callback(
    session: AsyncSession,
    *,
    user_id: UUID | None,
    session_token: str | None,
    state: str | None,
    iss: str | None,
    code: str | None,
    error: str | None,
    settings: Settings,
    client: SimklClient | None = None,
) -> CallbackOutcome:
    """Finish a connect. Checks run in this order, and the code is only exchanged after all pass:

    1. The `state` is looked up and deleted in one statement (single use, whatever follows). It
       must belong to this user and this browser session and not be expired.
    2. `iss` must equal `https://simkl.com`. Simkl sends `state` and `iss` on error callbacks
       too, so both are checked before an `error` is believed.
    3. An `error` (the user declined) ends the flow.
    4. The code is exchanged once. Simkl destroys a code on every failed attempt, so there is no
       retry.

    The caller commits the session. Tokens appear in no log line, URL or response.
    """
    try:
        _require_configured(settings)
    except SimklNotConfiguredError:
        return "error"
    if not state:
        return "error"
    pending = await simkl_repo.consume_oauth_state(session, state_hash=hash_token(state))
    # Commit the deletion now: the exchange below is a network call, and a failure after it must
    # not bring a spent state back.
    await session.commit()
    if (
        pending is None
        or user_id is None
        or not session_token
        or pending.user_id != user_id
        or pending.session_hash != hash_token(session_token)
        or pending.expires_at <= datetime.now(UTC)
    ):
        log.warning("simkl callback rejected: state is unknown, expired, or not this session's")
        return "error"
    if iss != SIMKL_ISSUER:
        log.warning("simkl callback rejected: issuer mismatch")
        return "error"
    if error:
        return "cancelled" if error == "access_denied" else "error"
    if not code:
        return "error"

    _, _, redirect_uri = _require_configured(settings)
    owns_client = client is None
    simkl = client or _make_client(settings)
    try:
        try:
            tokens = await simkl.exchange_code(
                code=code, redirect_uri=redirect_uri, code_verifier=pending.code_verifier
            )
        except SimklError as exc:
            log.warning("simkl code exchange failed: %s", exc)
            return "error"
        if not tokens.can_read:
            log.warning("simkl granted scope %r, which can't read the library", tokens.scope)
            await _revoke_quietly(simkl, tokens.refresh_token)
            return "error"
        try:
            await _store_grant(session, simkl, user_id=user_id, tokens=tokens, settings=settings)
        except Exception:
            # The grant is live at Simkl but not stored, so nothing would ever revoke it.
            log.exception("storing the simkl connection failed")
            await _revoke_quietly(simkl, tokens.refresh_token)
            raise
    finally:
        if owns_client:
            await simkl.aclose()
    return "connected"


async def _revoke_quietly(client: SimklClient, token: str) -> None:
    try:
        await client.revoke(token)
    except SimklError as exc:
        log.warning("simkl revoke failed: %s", exc)


async def _store_grant(
    session: AsyncSession,
    client: SimklClient,
    *,
    user_id: UUID,
    tokens: SimklTokens,
    settings: Settings,
) -> None:
    try:
        account = await client.get_user_settings(tokens.access_token)
    except SimklError as exc:
        # The name is only a label.
        log.warning("simkl account lookup failed: %s", exc)
        account = None
    encryptor = KeyEncryptor(settings.key_public_key or "")
    previous = await simkl_repo.get_connection(session, user_id=user_id, for_update=True)
    if previous is not None:
        # Each authorization is an independent grant. Revoke the one being replaced, in a worker.
        await _queue_revocation(
            session, user_id=user_id, refresh_token_ciphertext=previous.refresh_token_ciphertext
        )
    name = account.name if account else None
    source = await sources_repo.get_or_create_source(
        session,
        kind="simkl",
        owner_user_id=user_id,
        external_key=SIMKL_SOURCE_KEY,
        label=f"Simkl (@{name})" if name else "Simkl",
        url="https://simkl.com/",
    )
    await sources_service.subscribe_user_to_source(
        session, user_id=user_id, source=source, role=SIMKL_SOURCE_ROLE
    )
    await simkl_repo.upsert_connection(
        session,
        user_id=user_id,
        source_id=source.id,
        access_token_ciphertext=encryptor.encrypt(tokens.access_token),
        refresh_token_ciphertext=encryptor.encrypt(tokens.refresh_token),
        access_expires_at=datetime.now(UTC) + timedelta(seconds=tokens.expires_in),
        scope=tokens.scope,
        simkl_account_id=account.account_id if account else None,
        simkl_account_name=name,
    )
    await sources_repo.clear_source_error(session, source_id=source.id)
    # A new grant ends a pause. Jobs that waited for it run again; if there are none, start one.
    await keys_service.resume_key_and_jobs(session, user_id=user_id, key_type=KeyType.SIMKL)
    if not await jobs_repo.has_active_job(session, user_id=user_id, job_kind="simkl_sync"):
        await jobs_service.enqueue_job(
            session, user_id=user_id, job_kind="simkl_sync", trigger="connect"
        )


async def _queue_revocation(
    session: AsyncSession, *, user_id: UUID, refresh_token_ciphertext: bytes
) -> Job:
    """Move a sealed refresh token into `simkl_revocations` and queue the job that revokes it.

    The job payload names the row and holds nothing secret.
    """
    revocation = await simkl_repo.create_revocation(
        session, user_id=user_id, refresh_token_ciphertext=refresh_token_ciphertext
    )
    return await jobs_service.enqueue_job(
        session,
        user_id=user_id,
        job_kind="simkl_revoke",
        trigger="system",
        payload={"revocation_id": str(revocation.id)},
    )


# --- Sync and disconnect ----------------------------------------------------------------------


async def request_sync(session: AsyncSession, *, user_id: UUID) -> Job:
    """Queue a `simkl_sync` job for "Sync now".

    Raises `SimklNotConnectedError`, `SimklReconnectRequiredError`, or
    `jobs_service.DuplicateJobError` when one is queued, running or paused already.
    """
    connection = await simkl_repo.get_connection(session, user_id=user_id)
    if connection is None:
        raise SimklNotConnectedError("Simkl is not connected.")
    if connection.needs_reconnect:
        raise SimklReconnectRequiredError("Simkl needs to be reconnected before it can sync.")
    if await jobs_repo.has_active_job(session, user_id=user_id, job_kind="simkl_sync"):
        raise jobs_service.DuplicateJobError("a Simkl sync is already in progress")
    return await jobs_service.enqueue_job(session, user_id=user_id, job_kind="simkl_sync")


async def disconnect(session: AsyncSession, *, user_id: UUID) -> Job:
    """Drop the connection and everything it wrote, and queue the revocation at Simkl.

    The API can't read the tokens, so it can't revoke them. The sealed refresh token moves to
    `simkl_revocations` and a `simkl_revoke` job (payload: the row id) revokes it and deletes
    the row. The source goes now: its items, subscription and the history it wrote cascade, and
    the user's history is rebuilt from their other `self` sources (ADR 0009, ADR 0010).
    """
    connection = await simkl_repo.get_connection(session, user_id=user_id, for_update=True)
    if connection is None:
        raise SimklNotConnectedError("Simkl is not connected.")
    job = await _queue_revocation(
        session, user_id=user_id, refresh_token_ciphertext=connection.refresh_token_ciphertext
    )
    await jobs_repo.cancel_unstarted_jobs(session, user_id=user_id, job_kind="simkl_sync")
    await sources_repo.delete_source(session, source_id=connection.source_id)
    await sources_service.resync_watch_history(session, user_id=user_id)
    return job
