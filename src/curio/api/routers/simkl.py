"""Simkl connection routes: connect, the OAuth callback, status, sync, and disconnect.

The callback is a browser redirect from simkl.com, so it answers with redirects to the SPA
(`/app/sources?simkl=...`), never with JSON, and never puts a code or token in a URL. It needs
the same session cookie that started the flow: the cookie is `SameSite=Lax`, which the browser
sends on this top-level navigation.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from curio.api.deps import AppSettings, CurrentUser, DbSession
from curio.api.schemas.catalogs import MessageResponse
from curio.api.schemas.jobs import JobOut
from curio.api.schemas.simkl import SimklConnectResponse, SimklStatusResponse
from curio.crypto import PublicKeyNotConfiguredError
from curio.services import auth as auth_service
from curio.services import jobs as jobs_service
from curio.services import simkl as simkl_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simkl", tags=["simkl"])

SOURCES_PAGE = "/app/sources"


def _redirect(outcome: str) -> RedirectResponse:
    return RedirectResponse(url=f"{SOURCES_PAGE}?simkl={outcome}", status_code=303)


@router.get("/connection")
async def get_connection(
    user: CurrentUser, session: DbSession, settings: AppSettings
) -> SimklStatusResponse:
    result = await simkl_service.get_status(session, user_id=user.id, settings=settings)
    connection = result.connection
    return SimklStatusResponse(
        configured=result.configured,
        connected=connection is not None,
        source_id=connection.source_id if connection else None,
        account_name=connection.simkl_account_name if connection else None,
        last_synced_at=connection.last_synced_at if connection else None,
        last_error=connection.last_error if connection else None,
        needs_reconnect=connection.needs_reconnect if connection else False,
    )


@router.post("/connect")
async def connect(
    request: Request, user: CurrentUser, session: DbSession, settings: AppSettings
) -> SimklConnectResponse:
    """Start the OAuth flow. The browser then goes to `authorize_url` on simkl.com."""
    session_token = request.cookies.get(settings.session_cookie_name, "")
    try:
        authorize_url = await simkl_service.start_connect(
            session, user_id=user.id, session_token=session_token, settings=settings
        )
    except simkl_service.SimklNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
    except PublicKeyNotConfiguredError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Encryption public key is not configured on this server.",
        ) from None
    # Commit before responding: the callback may be served by another API process.
    await session.commit()
    return SimklConnectResponse(authorize_url=authorize_url)


@router.get("/callback", include_in_schema=False)
async def callback(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    state: str | None = None,
    iss: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> Response:
    """Simkl redirects the browser here. Always answers with a redirect to the Sources page."""
    session_token = request.cookies.get(settings.session_cookie_name)
    user = await auth_service.authenticate_session(session, session_token=session_token)
    try:
        outcome = await simkl_service.handle_callback(
            session,
            user_id=user.id if user else None,
            session_token=session_token,
            state=state,
            iss=iss,
            code=code,
            error=error,
            settings=settings,
        )
        await session.commit()
    except Exception:
        log.exception("simkl callback failed")
        await session.rollback()
        outcome = "error"
    return _redirect(outcome)


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_now(user: CurrentUser, session: DbSession) -> JobOut:
    """Queue a `simkl_sync` job. 404 if Simkl isn't connected, 409 if a sync is already active
    or the connection needs to be reconnected.
    """
    try:
        job = await simkl_service.request_sync(session, user_id=user.id)
    except simkl_service.SimklNotConnectedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (
        simkl_service.SimklReconnectRequiredError,
        jobs_service.DuplicateJobError,
        jobs_service.KeyPausedError,
    ) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    # Commit before responding: the client follows the job right away, maybe on another process.
    await session.commit()
    return JobOut.model_validate(job, from_attributes=True)


@router.delete("/connection")
async def disconnect(user: CurrentUser, session: DbSession) -> MessageResponse:
    """Remove the Simkl source and its history now. A worker revokes the grant at Simkl."""
    try:
        await simkl_service.disconnect(session, user_id=user.id)
    except simkl_service.SimklNotConnectedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    await session.commit()
    return MessageResponse(message="Simkl disconnected.")
