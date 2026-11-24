"""Background job enqueueing and worker execution orchestration.

A job is one unit of background work. Its `job_kind` selects a `JobSpec` from `JOB_REGISTRY`,
which names the pipeline recorded on the run, the API keys the job needs, and the handler that
does the work. Each attempt of a job gets its own run. A worker holds a lease on the job while it
runs it (see `process_one_job`).
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.letterboxd import LetterboxdError
from curio.clients.providers import KeyType
from curio.clients.ratelimit import RateLimiter
from curio.clients.simkl import SimklAuthError, SimklError
from curio.clients.tmdb import TmdbAuthError, TmdbError, TmdbRateLimitError
from curio.db.models.jobs import Job, Run, RunEvent
from curio.repositories import catalogs as catalogs_repo
from curio.repositories import jobs as jobs_repo
from curio.repositories import keys as keys_repo
from curio.repositories import simkl as simkl_repo
from curio.repositories import sources as sources_repo
from curio.services import keys as keys_service
from curio.services import simkl_sync, starter
from curio.services import sources as sources_service
from curio.services.keys import KeyNotFoundError
from curio.settings import Settings

log = logging.getLogger(__name__)

ACTIVE_JOB_CONSTRAINT = "uq_jobs_active_preset_kind"
LEASE_LOST_MESSAGE = "Worker stopped responding."
RETRY_BACKOFF_SECONDS = 10

_KEY_LABELS = {
    KeyType.TMDB: "TMDB",
    KeyType.GEMINI: "Gemini",
    KeyType.OPENAI: "OpenAI",
    KeyType.ANTHROPIC: "Anthropic",
    KeyType.OPENROUTER: "OpenRouter",
    KeyType.SIMKL: "Simkl",
}


class DuplicateJobError(Exception):
    """Raised when an active job already exists for the same preset and kind."""


class PresetNotFoundError(LookupError):
    """Raised when the target preset does not exist for the user."""


class UnknownJobKindError(ValueError):
    """Raised when enqueueing a job kind that has no registered handler."""


class MissingRequiredKeysError(ValueError):
    """Raised when a job cannot be enqueued because required API keys are not configured."""

    def __init__(self, missing_keys: list[str]) -> None:
        self.missing_keys = missing_keys
        super().__init__(f"missing required API keys: {', '.join(missing_keys)}")


class KeyPausedError(Exception):
    """Raised when a job cannot be enqueued because a required API key is paused."""

    def __init__(self, key_type: KeyType, reason: str) -> None:
        self.key_type = key_type
        self.reason = reason
        label = _KEY_LABELS.get(key_type, key_type.value)
        if key_type is KeyType.SIMKL:
            # Simkl is a connection, not a key: there is nothing to replace in Settings.
            super().__init__(f"Simkl connection paused: {reason}. Reconnect Simkl to resume.")
            return
        super().__init__(
            f"{label} key paused: {reason}. Replace your {label} key in Settings to resume."
        )


class PermanentJobError(Exception):
    """Raised by a handler for a failure that retrying cannot fix. The job fails at once.

    The message is shown to the user, so write it for them.
    """


# Run errors, `jobs.last_error` and run event messages are shown in the browser. Only these
# exception types have messages written for users; extend the tuple to expose another type.
# Every other exception is stored as a generic message and logged in full.
USER_FACING_ERRORS: tuple[type[Exception], ...] = (
    TmdbError,
    KeyNotFoundError,
    KeyPausedError,
    PermanentJobError,
    SimklError,
)


def safe_error_message(exc: BaseException) -> str:
    """Return the text of `exc` that may be stored and shown to the user."""
    if isinstance(exc, USER_FACING_ERRORS):
        return str(exc) or type(exc).__name__
    return f"Unexpected error ({type(exc).__name__}). See worker logs."


def key_type_for_exception(exc: BaseException) -> KeyType | None:
    """Return the API key whose failure `exc` reports, or `None` if it is not a key failure."""
    if isinstance(exc, KeyNotFoundError):
        return exc.key_type
    if isinstance(exc, TmdbAuthError | TmdbRateLimitError):
        return KeyType.TMDB
    if isinstance(exc, SimklAuthError):
        return KeyType.SIMKL
    return None


EmitStep = Callable[[str, str, dict[str, Any] | None], Awaitable[None]]


@dataclass(frozen=True)
class JobContext:
    """Everything a job handler receives."""

    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    rate_limiter: RateLimiter
    job: Job
    run: Run
    emit_step: EmitStep


JobHandler = Callable[[JobContext], Awaitable[None]]


@dataclass(frozen=True)
class JobSpec:
    pipeline: str
    required_keys: tuple[KeyType, ...]
    handler: JobHandler
    success_message: str


async def _run_starter_pipeline(ctx: JobContext) -> None:
    # Resolved at call time so tests can replace `starter.run_starter_pipeline`.
    await starter.run_starter_pipeline(
        ctx.session,
        session_factory=ctx.session_factory,
        settings=ctx.settings,
        rate_limiter=ctx.rate_limiter,
        job=ctx.job,
        run=ctx.run,
        emit_step=ctx.emit_step,
    )


async def _run_lb_import(ctx: JobContext) -> None:
    """Import an uploaded Letterboxd export. The stored upload is removed when the job ends."""
    try:
        await sources_service.run_lb_import_job(
            ctx.session,
            session_factory=ctx.session_factory,
            settings=ctx.settings,
            rate_limiter=ctx.rate_limiter,
            job=ctx.job,
            emit_step=ctx.emit_step,
        )
    except (LetterboxdError, sources_service.ImportJobInputError) as exc:
        await _discard_import_upload(ctx)
        raise PermanentJobError(str(exc)) from exc
    except Exception as exc:
        # A key failure pauses the job, and it will run again, so it still needs the upload.
        out_of_attempts = ctx.job.attempts >= ctx.job.max_attempts
        if key_type_for_exception(exc) is None and out_of_attempts:
            await _discard_import_upload(ctx)
        raise


async def _discard_import_upload(ctx: JobContext) -> None:
    try:
        upload_id, _ = sources_service.lb_import_job_input(ctx.job)
    except sources_service.ImportJobInputError:
        return
    await sources_service.discard_upload(
        ctx.session_factory, user_id=ctx.job.user_id, upload_id=upload_id
    )


async def _run_simkl_sync(ctx: JobContext) -> None:
    """Pull the user's Simkl library. A rejected grant pauses the job (`SimklAuthError`)."""
    try:
        await simkl_sync.run_sync_job(
            ctx.session,
            session_factory=ctx.session_factory,
            settings=ctx.settings,
            job=ctx.job,
            emit_step=ctx.emit_step,
        )
    except SimklError as exc:
        if exc.permanent:
            raise PermanentJobError(str(exc)) from exc
        raise


async def _run_simkl_revoke(ctx: JobContext) -> None:
    """Revoke a disconnected grant at Simkl and delete its sealed token."""
    try:
        await simkl_sync.run_revoke_job(
            ctx.session,
            session_factory=ctx.session_factory,
            settings=ctx.settings,
            job=ctx.job,
            emit_step=ctx.emit_step,
        )
    except Exception as exc:
        permanent = isinstance(exc, SimklError) and exc.permanent
        if permanent or ctx.job.attempts >= ctx.job.max_attempts:
            await simkl_sync.discard_revocation(ctx.session_factory, job=ctx.job)
        if permanent:
            raise PermanentJobError(str(exc)) from exc
        raise


# Registry of job kinds. To add one (for example `compile`):
#   1. Write `async def handler(ctx: JobContext) -> None`. Use `ctx.session` for database work
#      (commit when the work should survive a later failure), `ctx.emit_step(step, message,
#      payload)` for progress, and `ctx.job.payload` for job input. Raise to fail the attempt:
#      TMDB auth and rate-limit errors and `KeyNotFoundError` pause the job on that key, and
#      any other exception retries up to `max_attempts`. Only the exception types in
#      `USER_FACING_ERRORS` have their message shown to the user.
#   2. Add `"<kind>": JobSpec(pipeline=..., required_keys=(KeyType...,), handler=handler,
#      success_message=...)` below. `required_keys` is checked when the job is enqueued and
#      decides which key failures pause it.
#   3. Allow the kind in the `jobs_job_kind_check` constraint with a migration.
#   4. Enqueue it with `enqueue_job(session, user_id=..., job_kind="<kind>", ...)`.
JOB_REGISTRY: dict[str, JobSpec] = {
    "regenerate": JobSpec(
        pipeline="starter",
        required_keys=(KeyType.TMDB,),
        handler=_run_starter_pipeline,
        success_message="Catalog refreshed.",
    ),
    "lb_import": JobSpec(
        pipeline="lb_import",
        required_keys=(KeyType.TMDB,),
        handler=_run_lb_import,
        success_message="Letterboxd export imported.",
    ),
    "simkl_sync": JobSpec(
        pipeline="simkl_sync",
        required_keys=(KeyType.SIMKL,),
        handler=_run_simkl_sync,
        success_message="Simkl synced.",
    ),
    "simkl_revoke": JobSpec(
        pipeline="simkl_revoke",
        required_keys=(),
        handler=_run_simkl_revoke,
        success_message="Simkl access revoked.",
    ),
}


def _violated_constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


async def enqueue_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_kind: str,
    preset_id: UUID | None = None,
    trigger: str = "manual",
    payload: dict[str, Any] | None = None,
) -> Job:
    """Validate the job's preset and required keys, then enqueue it."""
    spec = JOB_REGISTRY.get(job_kind)
    if spec is None:
        raise UnknownJobKindError(f"unknown job kind '{job_kind}'")
    if preset_id is not None:
        preset = await catalogs_repo.get_preset(session, user_id=user_id, preset_id=preset_id)
        if preset is None:
            raise PresetNotFoundError(f"preset {preset_id} not found")
    required_keys = [key.value for key in spec.required_keys]
    # key type -> why it is paused (None when it is usable); absent when the user has none.
    available: dict[str, str | None] = {
        row.key_type: row.paused_reason
        for row in await keys_repo.list_api_key_metadata(session, user_id=user_id)
    }
    if KeyType.SIMKL.value in required_keys:
        connection = await simkl_repo.get_connection(session, user_id=user_id)
        if connection is not None:
            available[KeyType.SIMKL.value] = (
                (connection.last_error or "Simkl needs to be reconnected")
                if connection.needs_reconnect
                else None
            )
    missing = [key for key in required_keys if key not in available]
    if missing:
        raise MissingRequiredKeysError(missing)
    for key in required_keys:
        reason = available[key]
        if reason:
            raise KeyPausedError(KeyType(key), reason)
    try:
        return await jobs_repo.enqueue_job(
            session,
            user_id=user_id,
            preset_id=preset_id,
            job_kind=job_kind,
            trigger=trigger,
            required_keys=required_keys,
            payload=payload,
        )
    except IntegrityError as exc:
        if _violated_constraint(exc) == ACTIVE_JOB_CONSTRAINT:
            raise DuplicateJobError("an active job already exists for this preset") from exc
        raise


async def enqueue_regenerate_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
    trigger: str = "manual",
) -> Job:
    """Enqueue a `regenerate` job for a preset."""
    return await enqueue_job(
        session,
        user_id=user_id,
        job_kind="regenerate",
        preset_id=preset_id,
        trigger=trigger,
    )


async def enqueue_lb_import_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    filename: str,
    data: bytes,
    role: str,
) -> Job:
    """Store an uploaded export ZIP and enqueue the `lb_import` job that imports it.

    Raises `DuplicateJobError` if an import is already queued or running for the user, and the
    same key errors as `enqueue_job`. Nothing is stored in either case.
    """
    if await jobs_repo.has_active_job(session, user_id=user_id, job_kind="lb_import"):
        raise DuplicateJobError("an import is already in progress")
    async with session.begin_nested():
        upload = await sources_repo.create_upload(
            session, user_id=user_id, filename=filename, data=data
        )
        return await enqueue_job(
            session,
            user_id=user_id,
            job_kind="lb_import",
            payload={"upload_id": str(upload.id), "role": role},
        )


async def get_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
) -> Job | None:
    return await jobs_repo.get_job(session, user_id=user_id, job_id=job_id)


async def get_job_status(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
) -> tuple[Job, Run | None, list[RunEvent]] | None:
    job = await jobs_repo.get_job(session, user_id=user_id, job_id=job_id)
    if job is None:
        return None
    run = await jobs_repo.get_run_for_job(session, user_id=user_id, job_id=job_id)
    events = (
        await jobs_repo.list_run_events_for_run(session, user_id=user_id, run_id=run.id)
        if run is not None
        else []
    )
    return job, run, events


async def get_latest_preset_job_status(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> tuple[Job | None, Run | None, list[RunEvent]]:
    preset = await catalogs_repo.get_preset(session, user_id=user_id, preset_id=preset_id)
    if preset is None:
        raise PresetNotFoundError(f"preset {preset_id} not found")
    job = await jobs_repo.get_latest_preset_job(session, user_id=user_id, preset_id=preset_id)
    if job is None:
        return None, None, []
    run = await jobs_repo.get_run_for_job(session, user_id=user_id, job_id=job.id)
    events = (
        await jobs_repo.list_run_events_for_run(session, user_id=user_id, run_id=run.id)
        if run is not None
        else []
    )
    return job, run, events


async def list_run_events_for_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
    after_id: int = 0,
) -> list[RunEvent]:
    return await jobs_repo.list_run_events_for_job(
        session,
        user_id=user_id,
        job_id=job_id,
        after_id=after_id,
    )


async def _keep_lease_alive(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: UUID,
    worker_id: str,
    lease_seconds: int,
) -> None:
    """Renew the job's lease every third of its length, on short-lived sessions.

    Stops once the worker no longer owns the job. Runs until cancelled by `process_one_job`.
    """
    interval = lease_seconds / 3
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                owned = await jobs_repo.extend_job_lease(
                    session,
                    job_id=job_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
                await session.commit()
        except Exception:
            # A transient DB error: try again next tick. The lease has two more intervals.
            log.warning("job %s: could not extend lease", job_id, exc_info=True)
            continue
        if not owned:
            log.warning("job %s: lease lost, worker %s no longer owns it", job_id, worker_id)
            return


async def _finalize_claim(
    session: AsyncSession,
    *,
    job: Job,
    reclaimed: bool,
    spec: JobSpec | None,
    worker_id: str,
) -> bool:
    """Handle what a fresh claim can reveal before running. Returns `True` if the job is done.

    A reclaimed job's previous run is marked failed. A reclaimed job that has used all its
    attempts, or a job with an unregistered kind, fails permanently instead of running.
    """
    last_event_id: int | None = None
    if reclaimed:
        log.warning("job %s: reclaimed after lease expiry (attempt %d)", job.id, job.attempts)
        for stale in await jobs_repo.fail_running_runs(
            session, job_id=job.id, error=LEASE_LOST_MESSAGE
        ):
            event = await jobs_repo.append_run_event(
                session,
                run_id=stale.id,
                user_id=job.user_id,
                step="failed",
                message=f"Failed: {LEASE_LOST_MESSAGE}",
            )
            last_event_id = event.id
    final_error: str | None = None
    if reclaimed and job.attempts > job.max_attempts:
        final_error = LEASE_LOST_MESSAGE
    elif spec is None:
        final_error = f"Unknown job kind '{job.job_kind}'."
    if final_error is None:
        if last_event_id is not None:
            await jobs_repo.notify_job_event(
                session, job_id=job.id, event_id=last_event_id, status="running"
            )
        return False
    log.error("job %s failed without running: %s", job.id, final_error)
    await jobs_repo.finish_job(
        session,
        job_id=job.id,
        worker_id=worker_id,
        status="failed",
        last_error=final_error,
    )
    await jobs_repo.notify_job_event(
        session, job_id=job.id, event_id=last_event_id, status="failed"
    )
    return True


async def process_one_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    rate_limiter: RateLimiter,
    worker_id: str,
) -> bool:
    """Claim and execute a single job. Returns `False` if nothing was runnable.

    The claim sets a lease that a background task renews while the job runs. If the worker dies,
    the lease expires and another worker reclaims the job. Every later write to the job is
    fenced on `claimed_by`, so a worker that lost its job cannot overwrite the new owner's state.
    """
    async with session_factory() as claim_session:
        claimed = await jobs_repo.claim_next_job(
            claim_session,
            worker_id=worker_id,
            lease_seconds=settings.job_lease_seconds,
        )
        if claimed is None:
            return False
        job = claimed.job
        spec = JOB_REGISTRY.get(job.job_kind)
        done = await _finalize_claim(
            claim_session,
            job=job,
            reclaimed=claimed.reclaimed,
            spec=spec,
            worker_id=worker_id,
        )
        # Commit the claim immediately so other workers see `status = 'running'`.
        await claim_session.commit()
    if done or spec is None:
        return True
    heartbeat = asyncio.create_task(
        _keep_lease_alive(
            session_factory,
            job_id=job.id,
            worker_id=worker_id,
            lease_seconds=settings.job_lease_seconds,
        )
    )
    try:
        await _execute_job(
            session_factory,
            spec=spec,
            job=job,
            settings=settings,
            rate_limiter=rate_limiter,
            worker_id=worker_id,
        )
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
    return True


async def _execute_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    spec: JobSpec,
    job: Job,
    settings: Settings,
    rate_limiter: RateLimiter,
    worker_id: str,
) -> None:
    job_id = job.id
    user_id = job.user_id
    started = time.monotonic()
    run_id: UUID | None = None
    async with session_factory() as session:
        try:
            run = await jobs_repo.create_run(
                session,
                user_id=user_id,
                job_id=job_id,
                preset_id=job.preset_id,
                attempt=job.attempts,
                pipeline=spec.pipeline,
                trigger=job.trigger,
            )
            this_run_id = run.id
            run_id = this_run_id
            await session.commit()

            async def emit_step(
                step: str,
                message: str,
                payload: dict[str, Any] | None = None,
            ) -> None:
                """Persist a step event and NOTIFY SSE listeners immediately."""
                event = await jobs_repo.append_run_event(
                    session,
                    run_id=this_run_id,
                    user_id=user_id,
                    step=step,
                    message=message,
                    payload=payload,
                )
                await jobs_repo.notify_job_event(
                    session,
                    job_id=job_id,
                    event_id=event.id,
                    status="running",
                )
                # Commit per step so API processes listening on NOTIFY can read the row right away.
                await session.commit()

            await spec.handler(
                JobContext(
                    session=session,
                    session_factory=session_factory,
                    settings=settings,
                    rate_limiter=rate_limiter,
                    job=job,
                    run=run,
                    emit_step=emit_step,
                )
            )
            owned = await jobs_repo.finish_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                status="succeeded",
            )
            if not owned:
                await session.rollback()
                log.warning("job %s: finished but no longer owned by %s", job_id, worker_id)
                return
            await jobs_repo.finish_run(
                session,
                run_id=this_run_id,
                status="succeeded",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            event = await jobs_repo.append_run_event(
                session,
                run_id=this_run_id,
                user_id=user_id,
                step="completed",
                message=spec.success_message,
            )
            await jobs_repo.notify_job_event(
                session,
                job_id=job_id,
                event_id=event.id,
                status="succeeded",
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            await _record_failure(
                session_factory,
                exc,
                job=job,
                run_id=run_id,
                worker_id=worker_id,
                latency_ms=int((time.monotonic() - started) * 1000),
            )


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    exc: Exception,
    *,
    job: Job,
    run_id: UUID | None,
    worker_id: str,
    latency_ms: int,
) -> None:
    """Pause the job on a key failure, otherwise retry it or fail it for good.

    Every job and run write is fenced: if the worker no longer owns the job, only a log line is
    written. `run_id` is `None` when the run could not be created.
    """
    job_id = job.id
    message = safe_error_message(exc)
    key_type = key_type_for_exception(exc)
    async with session_factory() as session:
        if key_type is not None:
            log.warning("job %s paused on %s key: %s", job_id, key_type.value, message)
            # Pausing the key and the user's queued jobs stands even if this job was lost.
            await keys_service.pause_key_and_jobs(
                session,
                user_id=job.user_id,
                key_type=key_type,
                reason=message,
            )
            owned = await jobs_repo.pause_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                key_type=key_type.value,
                reason=message,
            )
            run_status, step, event_message, job_status = (
                "paused",
                "paused",
                f"Paused: {message}",
                "paused",
            )
        else:
            will_retry = job.attempts < job.max_attempts and not isinstance(exc, PermanentJobError)
            log.exception(
                "job %s failed (attempt %d/%d)",
                job_id,
                job.attempts,
                job.max_attempts,
                exc_info=exc,
            )
            job_status = "queued" if will_retry else "failed"
            owned = await jobs_repo.finish_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                status=job_status,
                last_error=message,
                run_at=(
                    datetime.now(UTC) + timedelta(seconds=RETRY_BACKOFF_SECONDS * job.attempts)
                    if will_retry
                    else None
                ),
            )
            run_status, step, event_message = "failed", "failed", f"Failed: {message}"
        if not owned:
            log.warning("job %s: lost to another worker; not recording %s", job_id, run_status)
        elif run_id is not None:
            await jobs_repo.finish_run(
                session,
                run_id=run_id,
                status=run_status,
                latency_ms=latency_ms,
                error=message,
            )
            event = await jobs_repo.append_run_event(
                session,
                run_id=run_id,
                user_id=job.user_id,
                step=step,
                message=event_message,
            )
            await jobs_repo.notify_job_event(
                session,
                job_id=job_id,
                event_id=event.id,
                status=job_status,
            )
        await session.commit()
