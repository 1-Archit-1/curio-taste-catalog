"""Authenticated control-plane routes (auth, settings, API keys, audit). No CORS."""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from curio.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    JobEvents,
    authenticate_request,
    get_session_factory,
)
from curio.api.job_events import TooManyStreamsError
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
from curio.api.schemas.sources import (
    CandidateMatchOut,
    ImportIssueListResponse,
    ImportIssueOut,
    ImportIssueResolveRequest,
    ImportIssueStatus,
    ImportItemKind,
    SourceCreateRequest,
    SourceKind,
    SourceListResponse,
    SourceOut,
    SourcePreviewItemOut,
    SourcePreviewRequest,
    SourcePreviewResponse,
    SourceRole,
    SourceUpdateRequest,
)
from curio.clients.letterboxd import MAX_ZIP_BYTES, LetterboxdError, LetterboxdNotFoundError
from curio.clients.providers import LLM_PROVIDERS, KeyType
from curio.crypto import PublicKeyNotConfiguredError
from curio.db.models.jobs import Job, Run, RunEvent
from curio.db.models.sources import ImportIssue
from curio.db.models.users import User
from curio.services import auth as auth_service
from curio.services import catalogs as catalogs_service
from curio.services import jobs as jobs_service
from curio.services import keys as keys_service
from curio.services import sources as sources_service
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


@router.post("/presets/{preset_id}/regenerate", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_preset(
    preset_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> JobOut:
    """Queue a background `regenerate` job for the preset.

    Returns 202 once the job is committed. 409 if one is already active or a required key is
    paused.
    """
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
    except (jobs_service.DuplicateJobError, jobs_service.KeyPausedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    # Commit before responding: the client opens the SSE stream right away, possibly on another
    # API worker, and must find the job.
    await session.commit()
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
_SSE_PING_SECONDS = 15.0


def _step_frame(event: RunEvent) -> str:
    return f"id: {event.id}\nevent: step\ndata: {_event_to_out(event).model_dump_json()}\n\n"


def _parse_last_event_id(raw: str | None) -> int:
    try:
        return max(int(raw), 0) if raw else 0
    except ValueError:
        return 0


@router.get(
    "/jobs/{job_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_job_events(
    job_id: UUID,
    request: Request,
    settings: AppSettings,
    hub: JobEvents,
) -> StreamingResponse:
    """Stream live job progress over SSE.

    Replays committed `run_events` after `Last-Event-ID` (or from the start), then waits for
    wake-ups from the API process's shared `job_events` listener until the job ends. Steps carry
    their event id, so a reconnecting `EventSource` resumes where it stopped. A stream ends after
    `sse_max_stream_seconds`; the browser then reconnects.

    This route holds no database session while streaming. Authentication and the job lookup use
    one short-lived session, and every wake-up reads on another.
    """
    session_factory = get_session_factory(request)
    async with session_factory() as auth_session:
        user = await authenticate_request(request, auth_session, settings)
        job = await jobs_service.get_job(auth_session, user_id=user.id, job_id=job_id)
        await auth_session.commit()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    user_id = user.id
    try:
        hub.acquire_stream(user_id, settings.sse_max_streams_per_user)
    except TooManyStreamsError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many open job streams. Close one and retry.",
        ) from exc
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            hub.release_stream(user_id)

    last_id = _parse_last_event_id(request.headers.get("last-event-id"))

    async def read_progress(after_id: int) -> tuple[list[RunEvent], Job | None]:
        async with session_factory() as read_session:
            events = await jobs_service.list_run_events_for_job(
                read_session,
                user_id=user_id,
                job_id=job_id,
                after_id=after_id,
            )
            current_job = await jobs_service.get_job(read_session, user_id=user_id, job_id=job_id)
        return events, current_job

    async def event_stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.sse_max_stream_seconds
        after_id = last_id
        try:
            # Subscribe before the first read so an event committed in between wakes us.
            async with hub.subscribe(job_id) as wakeups:
                while True:
                    events, current_job = await read_progress(after_id)
                    for ev in events:
                        after_id = ev.id
                        yield _step_frame(ev)
                    if current_job is None:
                        return
                    if current_job.status in _SSE_TERMINAL_STATUSES:
                        status_json = _job_to_out(current_job).model_dump_json()
                        yield f"event: status\ndata: {status_json}\n\n"
                        return
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return
                    try:
                        await asyncio.wait_for(
                            wakeups.get(), timeout=min(_SSE_PING_SECONDS, remaining)
                        )
                    except TimeoutError:
                        if loop.time() >= deadline:
                            return
                        yield ": ping\n\n"
        finally:
            release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
        # Covers a client that disconnects before the generator first runs.
        background=BackgroundTask(release),
    )


def _source_to_out(summary: sources_service.SourceSummary) -> SourceOut:
    s = summary.source
    us = summary.user_source
    return SourceOut(
        id=s.id,
        kind=cast(SourceKind, s.kind),
        external_key=s.external_key,
        label=s.label,
        url=s.url,
        role=cast(SourceRole, us.role),
        item_count=s.item_count,
        last_refreshed_at=s.last_refreshed_at,
        last_error=s.last_error,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _issue_to_out(issue: ImportIssue) -> ImportIssueOut:
    candidates = [
        CandidateMatchOut(
            media_type=cast(MediaType, c["media_type"]),
            tmdb_id=int(c["tmdb_id"]),
            name=str(c.get("name") or ""),
            year=int(c["year"]) if c.get("year") is not None else None,
            confidence=float(c.get("confidence") or 0.0),
        )
        for c in (issue.candidate_matches or [])
    ]
    return ImportIssueOut(
        id=issue.id,
        source_id=issue.source_id,
        item_kind=cast(ImportItemKind, issue.item_kind),
        raw_title=issue.raw_title,
        raw_year=issue.raw_year,
        letterboxd_url=issue.letterboxd_url,
        rating=issue.rating,
        watched_at=issue.watched_at,
        confidence=issue.confidence,
        candidate_matches=candidates,
        status=cast(ImportIssueStatus, issue.status),
        resolved_media_type=cast(MediaType | None, issue.resolved_media_type),
        resolved_tmdb_id=issue.resolved_tmdb_id,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


@router.get("/sources")
async def list_sources(user: CurrentUser, session: DbSession) -> SourceListResponse:
    items = await sources_service.list_user_sources(session, user_id=user.id)
    return SourceListResponse(sources=[_source_to_out(item) for item in items])


@router.post("/sources/preview")
async def preview_source(
    payload: SourcePreviewRequest,
    user: CurrentUser,
) -> SourcePreviewResponse:
    del user
    try:
        feed = await sources_service.preview_letterboxd_source(payload.input)
    except LetterboxdNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except LetterboxdError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    return SourcePreviewResponse(
        kind="lb_diary_rss",
        username=feed.username,
        label=f"Letterboxd RSS (@{feed.username})",
        url=feed.feed_url,
        entry_count=len(feed.entries),
        recent_titles=[
            SourcePreviewItemOut(
                title=e.title,
                year=e.year,
                rating=e.rating,
                watched_at=e.watched_at,
                tmdb_id=e.tmdb_id,
            )
            for e in feed.entries[:5]
        ],
    )


@router.post("/sources", status_code=status.HTTP_201_CREATED)
async def create_source(
    payload: SourceCreateRequest,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> SourceOut:
    """Subscribe to a public Letterboxd diary RSS feed. Needs no TMDB key."""
    try:
        summary = await sources_service.add_rss_source(
            session,
            user_id=user.id,
            input_value=payload.input,
            role=payload.role,
            settings=settings,
        )
    except LetterboxdNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except LetterboxdError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return _source_to_out(summary)


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: UUID,
    payload: SourceUpdateRequest,
    user: CurrentUser,
    session: DbSession,
) -> SourceOut:
    try:
        summary = await sources_service.update_source_role(
            session,
            user_id=user.id,
            source_id=source_id,
            role=payload.role,
        )
    except sources_service.SourceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return _source_to_out(summary)


@router.delete("/sources/{source_id}")
async def delete_source(
    source_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> MessageResponse:
    try:
        await sources_service.remove_user_source(
            session,
            user_id=user.id,
            source_id=source_id,
        )
    except sources_service.SourceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return MessageResponse(message="Source removed.")


@router.post("/sources/{source_id}/refresh")
async def refresh_source(
    source_id: UUID,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> SourceOut:
    """Fetch a public RSS source now. Needs no TMDB key."""
    try:
        summary = await sources_service.refresh_rss_source(
            session,
            session_factory=get_session_factory(request),
            source_id=source_id,
            user_id=user.id,
            settings=settings,
        )
    except sources_service.SourceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except sources_service.InvalidSourceOperationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except LetterboxdNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except LetterboxdError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return _source_to_out(summary)


# Starlette parses the multipart body before the route runs, so this limit is applied to a body
# that has been received. A reverse proxy should also cap request bodies (see docs/architecture.md).
_UPLOAD_READ_CHUNK = 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_UPLOAD_TOO_LARGE = "Export ZIP exceeds the 50 MB size limit."


async def _read_upload_limited(file: UploadFile) -> bytes:
    data = bytearray()
    while chunk := await file.read(_UPLOAD_READ_CHUNK):
        data.extend(chunk)
        if len(data) > MAX_ZIP_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=_UPLOAD_TOO_LARGE
            )
    return bytes(data)


@router.post("/sources/import-zip", status_code=status.HTTP_202_ACCEPTED)
async def import_zip_source(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    file: Annotated[UploadFile, File(...)],
    role: Annotated[SourceRole, Form()] = "self",
) -> JobOut:
    """Queue a background `lb_import` job for an uploaded Letterboxd export ZIP.

    Matching titles needs the user's TMDB key, which only workers can decrypt. Returns 202 with
    the job once it is committed; follow it at `/api/jobs/{id}`. 413 if the file is over 50 MB,
    422 if no TMDB key is configured, 409 if the key is paused or an import is already running.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > MAX_ZIP_BYTES + _MULTIPART_OVERHEAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=_UPLOAD_TOO_LARGE)
    data = await _read_upload_limited(file)
    try:
        job = await jobs_service.enqueue_lb_import_job(
            session,
            user_id=user.id,
            filename=(file.filename or "export.zip")[:255],
            data=data,
            role=role,
        )
    except jobs_service.MissingRequiredKeysError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except (jobs_service.DuplicateJobError, jobs_service.KeyPausedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    # Commit before responding: the client polls the job right away, possibly on another worker.
    await session.commit()
    return _job_to_out(job)


@router.get("/import-issues")
async def list_import_issues(
    user: CurrentUser,
    session: DbSession,
    issue_status: Annotated[ImportIssueStatus | None, Query(alias="status")] = None,
) -> ImportIssueListResponse:
    issues, pending_count = await sources_service.list_import_issues(
        session,
        user_id=user.id,
        status=issue_status,
    )
    return ImportIssueListResponse(
        issues=[_issue_to_out(issue) for issue in issues],
        pending_count=pending_count,
    )


@router.post("/import-issues/{issue_id}/resolve")
async def resolve_import_issue(
    issue_id: UUID,
    payload: ImportIssueResolveRequest,
    user: CurrentUser,
    session: DbSession,
) -> ImportIssueOut:
    """Match an import issue to a TMDB title. Stores a stub from the data at hand; no TMDB call."""
    try:
        issue = await sources_service.resolve_import_issue(
            session,
            user_id=user.id,
            issue_id=issue_id,
            media_type=payload.media_type,
            tmdb_id=payload.tmdb_id,
        )
    except sources_service.ImportIssueNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except sources_service.InvalidSourceOperationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _issue_to_out(issue)


@router.post("/import-issues/{issue_id}/dismiss")
async def dismiss_import_issue(
    issue_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> ImportIssueOut:
    try:
        issue = await sources_service.dismiss_import_issue(
            session,
            user_id=user.id,
            issue_id=issue_id,
        )
    except sources_service.ImportIssueNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return _issue_to_out(issue)
