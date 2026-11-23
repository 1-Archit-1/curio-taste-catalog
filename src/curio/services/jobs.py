"""Background job enqueueing and worker execution orchestration."""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.providers import KeyType
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import TmdbAuthError, TmdbRateLimitError
from curio.db.models.jobs import Job, Run, RunEvent
from curio.repositories import catalogs as catalogs_repo
from curio.repositories import jobs as jobs_repo
from curio.repositories import keys as keys_repo
from curio.services import keys as keys_service
from curio.services.keys import KeyNotFoundError
from curio.settings import Settings

log = logging.getLogger(__name__)

JOB_REQUIRED_KEYS: dict[str, list[str]] = {"regenerate": ["tmdb"]}


class DuplicateJobError(Exception):
    """Raised when an active job already exists for the same preset and kind."""


class PresetNotFoundError(LookupError):
    """Raised when the target preset does not exist for the user."""


class MissingRequiredKeysError(ValueError):
    """Raised when a job cannot be enqueued because required API keys are not configured."""

    def __init__(self, missing_keys: list[str]) -> None:
        self.missing_keys = missing_keys
        super().__init__(f"missing required API keys: {', '.join(missing_keys)}")


async def enqueue_regenerate_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
    trigger: str = "manual",
) -> Job:
    """Validate preset and required keys, then enqueue a `regenerate` job."""
    preset = await catalogs_repo.get_preset(session, user_id=user_id, preset_id=preset_id)
    if preset is None:
        raise PresetNotFoundError(f"preset {preset_id} not found")

    required_keys = JOB_REQUIRED_KEYS["regenerate"]
    configured = {
        row.key_type for row in await keys_repo.list_api_key_metadata(session, user_id=user_id)
    }
    missing = [key for key in required_keys if key not in configured]
    if missing:
        raise MissingRequiredKeysError(missing)

    try:
        return await jobs_repo.enqueue_job(
            session,
            user_id=user_id,
            preset_id=preset_id,
            job_kind="regenerate",
            trigger=trigger,
            required_keys=required_keys,
        )
    except IntegrityError as exc:
        raise DuplicateJobError("an active job already exists for this preset") from exc


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
        await jobs_repo.list_run_events_for_job(session, user_id=user_id, job_id=job_id)
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
        await jobs_repo.list_run_events_for_job(session, user_id=user_id, job_id=job.id)
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


async def process_one_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    rate_limiter: RateLimiter,
    worker_id: str,
) -> bool:
    """Claim and execute a single queued job. Returns `False` if the queue was empty."""
    from curio.services import starter

    async with session_factory() as claim_session:
        job = await jobs_repo.claim_next_job(claim_session, worker_id=worker_id)
        if job is None:
            return False
        # Commit the claim immediately so other workers see `status = 'running'`.
        await claim_session.commit()

    job_id = job.id
    user_id = job.user_id
    job_attempts = job.attempts
    job_max_attempts = job.max_attempts

    started = time.monotonic()
    async with session_factory() as session:
        run = await jobs_repo.create_run(
            session,
            user_id=user_id,
            job_id=job_id,
            preset_id=job.preset_id,
            pipeline="starter",
            trigger=job.trigger,
        )
        run_id = run.id
        await session.commit()

        async def emit_step(
            step: str,
            message: str,
            payload: dict[str, Any] | None = None,
        ) -> None:
            """Persist a step event and NOTIFY SSE listeners immediately."""
            event = await jobs_repo.append_run_event(
                session,
                run_id=run_id,
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

        try:
            await starter.run_starter_pipeline(
                session,
                session_factory=session_factory,
                settings=settings,
                rate_limiter=rate_limiter,
                job=job,
                run=run,
                emit_step=emit_step,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            await jobs_repo.finish_run(
                session,
                run_id=run_id,
                status="succeeded",
                latency_ms=latency_ms,
            )
            await jobs_repo.finish_job(
                session,
                job_id=job_id,
                status="succeeded",
            )
            event = await jobs_repo.append_run_event(
                session,
                run_id=run_id,
                user_id=user_id,
                step="completed",
                message="Catalog refreshed.",
            )
            await jobs_repo.notify_job_event(
                session,
                job_id=job_id,
                event_id=event.id,
                status="succeeded",
            )
            await session.commit()
        except (KeyNotFoundError, TmdbAuthError, TmdbRateLimitError) as exc:
            await session.rollback()
            latency_ms = int((time.monotonic() - started) * 1000)
            reason = str(exc) or type(exc).__name__
            log.warning("job %s paused on TMDB key: %s", job_id, reason)
            async with session_factory() as err_session:
                await keys_service.pause_key_and_jobs(
                    err_session,
                    user_id=user_id,
                    key_type=KeyType.TMDB,
                    reason=reason,
                )
                await jobs_repo.finish_run(
                    err_session,
                    run_id=run_id,
                    status="paused",
                    latency_ms=latency_ms,
                    error=reason,
                )
                event = await jobs_repo.append_run_event(
                    err_session,
                    run_id=run_id,
                    user_id=user_id,
                    step="paused",
                    message=f"Paused: {reason}",
                )
                await jobs_repo.notify_job_event(
                    err_session,
                    job_id=job_id,
                    event_id=event.id,
                    status="paused",
                )
                await err_session.commit()
        except Exception as exc:
            await session.rollback()
            latency_ms = int((time.monotonic() - started) * 1000)
            error_msg = str(exc) or type(exc).__name__
            log.exception(
                "job %s failed (attempt %d/%d)",
                job_id,
                job_attempts,
                job_max_attempts,
            )
            async with session_factory() as err_session:
                await jobs_repo.finish_run(
                    err_session,
                    run_id=run_id,
                    status="failed",
                    latency_ms=latency_ms,
                    error=error_msg,
                )
                if job_attempts < job_max_attempts:
                    next_status = "queued"
                    next_run_at = datetime.now(UTC) + timedelta(seconds=10 * job_attempts)
                    await jobs_repo.finish_job(
                        err_session,
                        job_id=job_id,
                        status=next_status,
                        last_error=error_msg,
                        run_at=next_run_at,
                    )
                else:
                    next_status = "failed"
                    await jobs_repo.finish_job(
                        err_session,
                        job_id=job_id,
                        status=next_status,
                        last_error=error_msg,
                    )
                event = await jobs_repo.append_run_event(
                    err_session,
                    run_id=run_id,
                    user_id=user_id,
                    step="failed",
                    message=f"Failed: {error_msg}",
                )
                await jobs_repo.notify_job_event(
                    err_session,
                    job_id=job_id,
                    event_id=event.id,
                    status=next_status,
                )
                await err_session.commit()

    return True
