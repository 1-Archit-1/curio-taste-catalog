"""Background job queue, execution run, and run event persistence."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.jobs import Job, Run, RunEvent

_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


async def enqueue_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID | None,
    job_kind: str,
    trigger: str,
    required_keys: list[str],
    payload: dict[str, Any] | None = None,
    run_at: datetime | None = None,
) -> Job:
    """Insert a new queued job inside a savepoint so IntegrityError leaves the outer txn intact."""
    values: dict[str, Any] = {
        "user_id": user_id,
        "preset_id": preset_id,
        "job_kind": job_kind,
        "trigger": trigger,
        "required_keys": required_keys,
    }
    if payload is not None:
        values["payload"] = payload
    if run_at is not None:
        values["run_at"] = run_at

    async with session.begin_nested():
        stmt = insert(Job).values(**values).returning(Job)
        return (await session.execute(stmt)).scalar_one()


async def get_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
) -> Job | None:
    stmt = select(Job).where(Job.user_id == user_id, Job.id == job_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_active_job(session: AsyncSession, *, user_id: UUID, job_kind: str) -> bool:
    """Whether the user has a `queued`, `running` or `paused` job of this kind."""
    stmt = select(
        select(Job.id)
        .where(
            Job.user_id == user_id,
            Job.job_kind == job_kind,
            Job.status.in_(("queued", "running", "paused")),
        )
        .exists()
    )
    return bool(await session.scalar(stmt))


async def get_running_job(session: AsyncSession, *, user_id: UUID, job_kind: str) -> Job | None:
    """The user's newest `queued` or `running` job of this kind. A paused one waits on the user."""
    stmt = (
        select(Job)
        .where(
            Job.user_id == user_id,
            Job.job_kind == job_kind,
            Job.status.in_(("queued", "running")),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_latest_preset_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    preset_id: UUID,
) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.user_id == user_id, Job.preset_id == preset_id)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True)
class ClaimedJob:
    job: Job
    # True when the job was `running` with an expired lease, i.e. taken over from a dead worker.
    reclaimed: bool


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
) -> ClaimedJob | None:
    """Claim the next runnable job using SKIP LOCKED.

    Runnable means `queued` and due, or `running` with an expired lease (its worker died or
    stopped renewing). The claim sets a fresh lease and counts one attempt.
    """
    pick_stmt = (
        select(Job.id, Job.status)
        .where(
            or_(
                and_(Job.status == "queued", Job.run_at <= func.now()),
                and_(Job.status == "running", Job.lease_expires_at < func.now()),
            )
        )
        .order_by(Job.run_at.asc(), Job.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    picked = (await session.execute(pick_stmt)).one_or_none()
    if picked is None:
        return None
    update_stmt = (
        update(Job)
        .where(Job.id == picked.id)
        .values(
            status="running",
            claimed_by=worker_id,
            claimed_at=func.now(),
            lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
            attempts=Job.attempts + 1,
            updated_at=func.now(),
        )
        .returning(Job)
    )
    job = (await session.execute(update_stmt)).scalar_one()
    return ClaimedJob(job=job, reclaimed=picked.status == "running")


async def extend_job_lease(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """Push the lease out. Returns `False` if `worker_id` no longer owns the running job."""
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.claimed_by == worker_id)
        .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
    )
    return _updated(await session.execute(stmt))


async def get_run_for_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
) -> Run | None:
    """Return the job's latest run (one run exists per attempt)."""
    stmt = (
        select(Run)
        .where(Run.user_id == user_id, Run.job_id == job_id)
        .order_by(Run.started_at.desc(), Run.attempt.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_run(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
    preset_id: UUID | None,
    attempt: int,
    pipeline: str,
    trigger: str,
) -> Run:
    stmt = (
        insert(Run)
        .values(
            user_id=user_id,
            job_id=job_id,
            preset_id=preset_id,
            attempt=attempt,
            pipeline=pipeline,
            trigger=trigger,
            status="running",
        )
        .returning(Run)
    )
    return (await session.execute(stmt)).scalar_one()


async def finish_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    status: str,
    latency_ms: int,
    error: str | None = None,
) -> None:
    stmt = (
        update(Run)
        .where(Run.id == run_id)
        .values(
            status=status,
            latency_ms=latency_ms,
            error=error,
            finished_at=func.now(),
        )
    )
    await session.execute(stmt)


def _updated_count(result: Any) -> int:
    return int(result.rowcount or 0)


def _updated(result: Any) -> bool:
    return _updated_count(result) > 0


async def fail_running_runs(
    session: AsyncSession,
    *,
    job_id: UUID,
    error: str,
) -> list[Run]:
    """Mark a job's still-`running` runs as failed (their worker is gone)."""
    stmt = (
        update(Run)
        .where(Run.job_id == job_id, Run.status == "running")
        .values(status="failed", error=error, finished_at=func.now())
        .returning(Run)
    )
    return list((await session.execute(stmt)).scalars().all())


async def finish_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    status: str,
    last_error: str | None = None,
    run_at: datetime | None = None,
) -> bool:
    """Mark a job terminal (`succeeded`/`failed`/`cancelled`) or requeue it.

    Fenced: only applies while `worker_id` still owns the running job. Returns `False` if the
    job was reclaimed or otherwise changed, in which case nothing is written.
    """
    values: dict[str, Any] = {
        "status": status,
        "last_error": last_error,
        "lease_expires_at": None,
        "updated_at": func.now(),
    }
    if status in _TERMINAL_JOB_STATUSES:
        values["finished_at"] = func.now()
    else:
        values["claimed_by"] = None
        values["claimed_at"] = None
        if run_at is not None:
            values["run_at"] = run_at
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.claimed_by == worker_id)
        .values(**values)
    )
    return _updated(await session.execute(stmt))


async def pause_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    key_type: str,
    reason: str,
) -> bool:
    """Pause the job `worker_id` is running, handing back the attempt it consumed (fenced)."""
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.claimed_by == worker_id)
        .values(
            status="paused",
            paused_key_type=key_type,
            paused_reason=reason,
            attempts=func.greatest(Job.attempts - 1, 0),
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            updated_at=func.now(),
        )
    )
    return _updated(await session.execute(stmt))


async def append_run_event(
    session: AsyncSession,
    *,
    run_id: UUID,
    user_id: UUID,
    step: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> RunEvent:
    stmt = (
        insert(RunEvent)
        .values(
            run_id=run_id,
            user_id=user_id,
            step=step,
            message=message,
            payload=payload,
        )
        .returning(RunEvent)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_run_events_for_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
    after_id: int = 0,
) -> list[RunEvent]:
    """Events from every run of the job, by event id (what SSE streams)."""
    stmt = (
        select(RunEvent)
        .join(Run, RunEvent.run_id == Run.id)
        .where(
            Run.job_id == job_id,
            Run.user_id == user_id,
            RunEvent.id > after_id,
        )
        .order_by(RunEvent.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_run_events_for_run(
    session: AsyncSession,
    *,
    user_id: UUID,
    run_id: UUID,
) -> list[RunEvent]:
    stmt = (
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.user_id == user_id)
        .order_by(RunEvent.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


# One shared channel; the payload carries the job id so each API process needs a single LISTEN.
JOB_EVENTS_CHANNEL = "job_events"


async def notify_job_event(
    session: AsyncSession,
    *,
    job_id: UUID,
    event_id: int | None,
    status: str,
) -> None:
    """NOTIFY `job_events`; delivered to API processes when the transaction commits."""
    payload = json.dumps({"job_id": str(job_id), "event_id": event_id, "status": status})
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": JOB_EVENTS_CHANNEL, "payload": payload},
    )


async def pause_user_jobs_for_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
    reason: str,
) -> list[UUID]:
    """Pause the `queued` jobs of `user_id` that require `key_type`.

    `running` jobs are left alone: each pauses itself, through the fenced `pause_job`, when it
    hits the key error.
    """
    stmt = (
        update(Job)
        .where(
            Job.user_id == user_id,
            Job.status == "queued",
            Job.required_keys.contains([key_type]),
        )
        .values(
            status="paused",
            paused_key_type=key_type,
            paused_reason=reason,
            updated_at=func.now(),
        )
        .returning(Job.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def resume_user_jobs_for_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
) -> list[UUID]:
    """Requeue paused jobs for `user_id` that were paused on `key_type`."""
    stmt = (
        update(Job)
        .where(
            Job.user_id == user_id,
            Job.status == "paused",
            Job.paused_key_type == key_type,
        )
        .values(
            status="queued",
            paused_key_type=None,
            paused_reason=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            run_at=func.now(),
            updated_at=func.now(),
        )
        .returning(Job.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def cancel_unstarted_jobs(session: AsyncSession, *, user_id: UUID, job_kind: str) -> int:
    """Cancel the user's `queued` and `paused` jobs of one kind. A `running` job is left alone."""
    stmt = (
        update(Job)
        .where(
            Job.user_id == user_id,
            Job.job_kind == job_kind,
            Job.status.in_(("queued", "paused")),
        )
        .values(status="cancelled", finished_at=func.now(), updated_at=func.now())
    )
    return _updated_count(await session.execute(stmt))
