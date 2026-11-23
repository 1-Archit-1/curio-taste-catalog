"""Authenticated control-plane routes (auth, settings, API keys, audit). No CORS."""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from curio.api.deps import AppSettings, CurrentUser, DbSession
from curio.api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    UserSummaryResponse,
)
from curio.api.schemas.catalogs import (
    CandidateListResponse,
    CandidateOut,
    MediaType,
    MessageResponse,
    PresetCreateRequest,
    PresetListResponse,
    PresetOut,
    PresetUpdateRequest,
    SlotAssignRequest,
    SlotListResponse,
    SlotOut,
    VetoKind,
    VetoOut,
    VetoRequest,
)
from curio.api.schemas.jobs import (
    JobOut,
    JobStatusResponse,
    LatestPresetJobResponse,
    RunEventOut,
    RunOut,
)
from curio.api.schemas.keys import (
    ApiKeyRequest,
    AuditEntryResponse,
    AuditLogResponse,
    KeyDeletedResponse,
    KeySavedResponse,
    KeyStatusResponse,
    KeyTestResponse,
)
from curio.api.schemas.settings import (
    RotateAddonTokenResponse,
    SettingsResponse,
    UpdateSettingsRequest,
)
from curio.clients.providers import LLM_PROVIDERS, KeyType
from curio.crypto import PublicKeyNotConfiguredError
from curio.db.models.jobs import Job, Run, RunEvent
from curio.db.models.users import User
from curio.services import auth as auth_service
from curio.services import catalogs as catalogs_service
from curio.services import jobs as jobs_service
from curio.services import keys as keys_service
from curio.services import users as users_service
from curio.settings import Settings

router = APIRouter(prefix="/api", tags=["control"])


def _manifest_url(request: Request, settings: Settings, addon_token: str) -> str:
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/{addon_token}/manifest.json"


async def _build_settings_response(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    user: User,
) -> SettingsResponse:
    metadata = await keys_service.list_user_key_metadata(session, user_id=user.id)
    keys_payload: dict[KeyType, KeyStatusResponse] = {}
    for kt in KeyType:
        row = metadata.get(kt)
        keys_payload[kt] = KeyStatusResponse(
            configured=row is not None,
            last4=row.last4 if row else None,
            updated_at=row.updated_at if row else None,
            paused_reason=row.paused_reason if row else None,
        )

    return SettingsResponse(
        user_id=user.id,
        username=user.username,
        addon_manifest_url=_manifest_url(request, settings, user.addon_token),
        llm_provider=KeyType(user.llm_provider),
        supported_llm_providers=list(LLM_PROVIDERS),
        monthly_token_budget=user.monthly_token_budget,
        jobs_paused_reason=user.jobs_paused_reason,
        keys=keys_payload,
    )


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> LoginResponse:
    result = await auth_service.exchange_login_token(
        session,
        login_token=payload.token,
        ttl_days=settings.session_ttl_days,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid login token.",
        )

    user, session_token = result
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=settings.session_ttl_days * 86400,
        path="/",
    )
    return LoginResponse(user=UserSummaryResponse(id=user.id, username=user.username))


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> LogoutResponse:
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        await auth_service.revoke_session(session, user_id=user.id, session_token=session_token)
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return LogoutResponse(ok=True)


@router.get("/auth/me")
async def me(user: CurrentUser) -> UserSummaryResponse:
    return UserSummaryResponse(id=user.id, username=user.username)


@router.get("/settings")
async def get_settings_view(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> SettingsResponse:
    return await _build_settings_response(request, session, settings, user)


@router.patch("/settings")
async def update_settings_view(
    payload: UpdateSettingsRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> SettingsResponse:
    try:
        updated = await users_service.update_preferences(
            session,
            user_id=user.id,
            llm_provider=payload.llm_provider,
            monthly_token_budget=payload.monthly_token_budget,
        )
    except (users_service.InvalidProviderError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return await _build_settings_response(request, session, settings, updated)


@router.post("/settings/addon-token/rotate")
async def rotate_addon_token(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> RotateAddonTokenResponse:
    updated = await users_service.rotate_addon_token_for_user(session, user_id=user.id)
    return RotateAddonTokenResponse(
        addon_manifest_url=_manifest_url(request, settings, updated.addon_token),
    )


@router.post("/settings/keys/{key_type}/test")
async def test_api_key(
    key_type: KeyType,
    payload: ApiKeyRequest,
    _: CurrentUser,
) -> KeyTestResponse:
    result = await keys_service.test_api_key(key_type, payload.api_key)
    return KeyTestResponse(
        ok=result.ok,
        status_code=result.status_code,
        detail=result.detail,
    )


@router.put("/settings/keys/{key_type}")
async def save_api_key(
    key_type: KeyType,
    payload: ApiKeyRequest,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> KeySavedResponse:
    try:
        row = await keys_service.set_user_api_key(
            session,
            user_id=user.id,
            key_type=key_type,
            api_key=payload.api_key,
            public_key=settings.key_public_key,
        )
    except PublicKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Encryption public key is not configured on this server.",
        ) from exc
    except keys_service.KeyValidationFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return KeySavedResponse(
        key_type=KeyType(row.key_type),
        configured=True,
        last4=row.last4,
        updated_at=row.updated_at,
    )


@router.delete("/settings/keys/{key_type}")
async def delete_api_key(
    key_type: KeyType,
    user: CurrentUser,
    session: DbSession,
) -> KeyDeletedResponse:
    deleted = await keys_service.delete_user_api_key(session, user_id=user.id, key_type=key_type)
    return KeyDeletedResponse(deleted=deleted)


@router.get("/settings/audit")
async def get_audit_log(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditLogResponse:
    entries = await keys_service.list_user_audit_entries(session, user_id=user.id, limit=limit)
    return AuditLogResponse(
        entries=[
            AuditEntryResponse(
                id=entry.id,
                key_type=KeyType(entry.key_type),
                job_id=entry.job_id,
                reason=entry.reason,
                created_at=entry.created_at,
            )
            for entry in entries
        ]
    )


def _preset_to_out(preset: catalogs_service.Preset) -> PresetOut:
    return PresetOut(
        id=preset.id,
        name=preset.name,
        media_type=cast(MediaType, preset.media_type),
        min_rating=preset.min_rating,
        criteria=preset.criteria,
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


def _slot_to_out(slot: catalogs_service.SlotRow) -> SlotOut:
    return SlotOut(
        slot_id=slot.slot_id,
        name=slot.name,
        media_type=slot.media_type,
        preset_id=slot.preset_id,
        preset_name=slot.preset_name,
    )


@router.get("/slots")
async def list_slots(user: CurrentUser, session: DbSession) -> SlotListResponse:
    slots = await catalogs_service.list_slots(session, user_id=user.id)
    return SlotListResponse(slots=[_slot_to_out(slot) for slot in slots])


@router.put("/slots/{slot_id}")
async def assign_slot(
    slot_id: str,
    payload: SlotAssignRequest,
    user: CurrentUser,
    session: DbSession,
) -> SlotOut:
    if slot_id not in catalogs_service.FIXED_SLOTS_BY_ID:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown slot '{slot_id}'.",
        )
    try:
        slot = await catalogs_service.assign_slot(
            session,
            user_id=user.id,
            slot_id=slot_id,
            preset_id=payload.preset_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return _slot_to_out(slot)


@router.get("/presets")
async def list_presets(user: CurrentUser, session: DbSession) -> PresetListResponse:
    presets = await catalogs_service.list_presets(session, user_id=user.id)
    return PresetListResponse(presets=[_preset_to_out(p) for p in presets])


@router.post("/presets", status_code=status.HTTP_201_CREATED)
async def create_preset(
    payload: PresetCreateRequest,
    user: CurrentUser,
    session: DbSession,
) -> PresetOut:
    preset = await catalogs_service.create_preset(
        session,
        user_id=user.id,
        name=payload.name,
        media_type=payload.media_type,
        min_rating=payload.min_rating,
        criteria=payload.criteria,
    )
    return _preset_to_out(preset)


@router.patch("/presets/{preset_id}")
async def update_preset(
    preset_id: UUID,
    payload: PresetUpdateRequest,
    user: CurrentUser,
    session: DbSession,
) -> PresetOut:
    updates = payload.model_dump(exclude_unset=True)
    preset = await catalogs_service.update_preset(
        session,
        user_id=user.id,
        preset_id=preset_id,
        **updates,
    )
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preset not found.",
        )
    return _preset_to_out(preset)


@router.delete("/presets/{preset_id}")
async def delete_preset(
    preset_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> MessageResponse:
    deleted = await catalogs_service.delete_preset(
        session,
        user_id=user.id,
        preset_id=preset_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preset not found.",
        )
    return MessageResponse(message="Preset deleted.")


@router.get("/presets/{preset_id}/candidates")
async def list_preset_candidates(
    preset_id: UUID,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> CandidateListResponse:
    preset = await catalogs_service.get_preset(
        session,
        user_id=user.id,
        preset_id=preset_id,
    )
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preset not found.",
        )
    rows = await catalogs_service.list_preset_candidates(
        session,
        user_id=user.id,
        preset_id=preset_id,
    )
    base_url = settings.public_base_url or str(request.base_url)
    return CandidateListResponse(
        preset_id=preset_id,
        candidates=[
            CandidateOut(
                media_type=c.media_type,
                tmdb_id=c.tmdb_id,
                imdb_id=c.imdb_id,
                name=c.name,
                year=c.year,
                poster=catalogs_service.resolve_poster_url(c.poster_url, base_url=base_url),
                description=c.description,
                rating=c.rating,
                blurb=c.blurb,
                position=c.position,
                watch_status=c.watch_status,
                veto_kind=c.veto_kind,
            )
            for c in rows
        ],
    )


@router.put("/vetoes/{media_type}/{tmdb_id}")
async def upsert_veto(
    media_type: MediaType,
    tmdb_id: int,
    payload: VetoRequest,
    user: CurrentUser,
    session: DbSession,
) -> VetoOut:
    try:
        veto = await catalogs_service.upsert_veto(
            session,
            user_id=user.id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            kind=payload.kind,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return VetoOut(
        media_type=cast(MediaType, veto.media_type),
        tmdb_id=veto.tmdb_id,
        kind=cast(VetoKind, veto.kind),
    )


@router.delete("/vetoes/{media_type}/{tmdb_id}")
async def delete_veto(
    media_type: MediaType,
    tmdb_id: int,
    user: CurrentUser,
    session: DbSession,
) -> MessageResponse:
    await catalogs_service.delete_veto(
        session,
        user_id=user.id,
        media_type=media_type,
        tmdb_id=tmdb_id,
    )
    return MessageResponse(message="Veto removed.")


def _job_to_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        preset_id=job.preset_id,
        job_kind=job.job_kind,
        status=job.status,
        trigger=job.trigger,
        paused_key_type=job.paused_key_type,
        paused_reason=job.paused_reason,
        last_error=job.last_error,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _run_to_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        pipeline=run.pipeline,
        trigger=run.trigger,
        status=run.status,
        latency_ms=run.latency_ms,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _event_to_out(event: RunEvent) -> RunEventOut:
    return RunEventOut(
        id=event.id,
        step=event.step,
        message=event.message,
        payload=event.payload,
        created_at=event.created_at,
    )


@router.post("/presets/{preset_id}/regenerate", status_code=status.HTTP_201_CREATED)
async def regenerate_preset(
    preset_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> JobOut:
    """Enqueue a background `regenerate` job for the preset (409 if one is already active)."""
    try:
        job = await jobs_service.enqueue_regenerate_job(
            session,
            user_id=user.id,
            preset_id=preset_id,
        )
    except jobs_service.PresetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except jobs_service.MissingRequiredKeysError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except jobs_service.DuplicateJobError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _job_to_out(job)


@router.get("/presets/{preset_id}/jobs/latest")
async def get_latest_preset_job(
    preset_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> LatestPresetJobResponse:
    """Return the most recent job, run, and step events for a preset."""
    try:
        job, run, events = await jobs_service.get_latest_preset_job_status(
            session,
            user_id=user.id,
            preset_id=preset_id,
        )
    except jobs_service.PresetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return LatestPresetJobResponse(
        job=_job_to_out(job) if job is not None else None,
        run=_run_to_out(run) if run is not None else None,
        events=[_event_to_out(ev) for ev in events],
    )


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> JobStatusResponse:
    """Return a single job with its run summary and recorded step events."""
    result = await jobs_service.get_job_status(
        session,
        user_id=user.id,
        job_id=job_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    job, run, events = result
    return JobStatusResponse(
        job=_job_to_out(job),
        run=_run_to_out(run) if run is not None else None,
        events=[_event_to_out(ev) for ev in events],
    )


_SSE_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "paused"})


@router.get(
    "/jobs/{job_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_job_events(
    job_id: UUID,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> StreamingResponse:
    """Stream live job progress over SSE using Postgres LISTEN/NOTIFY.

    Replays any already-committed `run_events` first so steps emitted before the
    browser connected are never lost, then wakes on `NOTIFY` until the job ends.
    """
    # FastAPI runs `get_session`'s commit after sending the 201 response, so an immediate
    # SSE connection on another API worker may arrive a few milliseconds before commit finishes.
    job = await jobs_service.get_job(session, user_id=user.id, job_id=job_id)
    if job is None:
        session_factory = getattr(request.app.state, "session_factory", None)
        if session_factory is not None:
            for _ in range(10):
                await asyncio.sleep(0.05)
                async with session_factory() as retry_session:
                    job = await jobs_service.get_job(retry_session, user_id=user.id, job_id=job_id)
                if job is not None:
                    break
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    session_factory = request.app.state.session_factory
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    async def event_stream() -> AsyncIterator[str]:
        # Dedicated unpooled connection so long-lived LISTEN never exhausts the SQLAlchemy pool.
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        try:
            await conn.execute(f"LISTEN job_events_{job_id.hex}")
            last_id = 0

            # Initial replay: catch events committed before LISTEN started.
            async with session_factory() as read_session:
                events = await jobs_service.list_run_events_for_job(
                    read_session,
                    user_id=user.id,
                    job_id=job_id,
                    after_id=last_id,
                )
                current_job = await jobs_service.get_job(
                    read_session,
                    user_id=user.id,
                    job_id=job_id,
                )

            for ev in events:
                last_id = ev.id
                yield f"event: step\ndata: {_event_to_out(ev).model_dump_json()}\n\n"

            if current_job is None or current_job.status in _SSE_TERMINAL_STATUSES:
                if current_job is not None:
                    yield f"event: status\ndata: {_job_to_out(current_job).model_dump_json()}\n\n"
                return

            while True:
                # Block until the worker commits + NOTIFYs, or send a keepalive comment every 15s.
                received = False
                async for _notify in conn.notifies(timeout=15.0, stop_after=1):
                    received = True
                if not received:
                    yield ": ping\n\n"

                async with session_factory() as read_session:
                    events = await jobs_service.list_run_events_for_job(
                        read_session,
                        user_id=user.id,
                        job_id=job_id,
                        after_id=last_id,
                    )
                    current_job = await jobs_service.get_job(
                        read_session,
                        user_id=user.id,
                        job_id=job_id,
                    )

                for ev in events:
                    last_id = ev.id
                    yield f"event: step\ndata: {_event_to_out(ev).model_dump_json()}\n\n"

                if current_job is None or current_job.status in _SSE_TERMINAL_STATUSES:
                    if current_job is not None:
                        status_json = _job_to_out(current_job).model_dump_json()
                        yield f"event: status\ndata: {status_json}\n\n"
                    return
        finally:
            await conn.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
