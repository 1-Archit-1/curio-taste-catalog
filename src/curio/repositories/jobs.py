"""Background job queue, execution run, and run event persistence."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, text, update
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


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str,
) -> Job | None:
    """Claim the next runnable queued job using SKIP LOCKED."""
    pick_stmt = (
        select(Job.id)
        .where(Job.status == "queued", Job.run_at <= func.now())
        .order_by(Job.run_at.asc(), Job.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job_id = (await session.execute(pick_stmt)).scalar_one_or_none()
    if job_id is None:
        return None

    update_stmt = (
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="running",
            claimed_by=worker_id,
            claimed_at=func.now(),
            attempts=Job.attempts + 1,
            updated_at=func.now(),
        )
        .returning(Job)
    )
    return (await session.execute(update_stmt)).scalar_one()


async def get_run_for_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
) -> Run | None:
    stmt = select(Run).where(Run.user_id == user_id, Run.job_id == job_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_run(
    session: AsyncSession,
    *,
    user_id: UUID,
    job_id: UUID,
    preset_id: UUID | None,
    pipeline: str,
    trigger: str,
) -> Run:
    stmt = (
        insert(Run)
        .values(
            user_id=user_id,
            job_id=job_id,
            preset_id=preset_id,
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


async def finish_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    status: str,
    last_error: str | None = None,
    run_at: datetime | None = None,
) -> None:
    """Mark a job terminal (`succeeded`/`failed`/`cancelled`) or requeue it."""
    values: dict[str, Any] = {
        "status": status,
        "last_error": last_error,
        "updated_at": func.now(),
    }
    if status in _TERMINAL_JOB_STATUSES:
        values["finished_at"] = func.now()
    else:
        values["claimed_by"] = None
        values["claimed_at"] = None
        if run_at is not None:
            values["run_at"] = run_at

    stmt = update(Job).where(Job.id == job_id).values(**values)
    await session.execute(stmt)


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


async def notify_job_event(
    session: AsyncSession,
    *,
    job_id: UUID,
    event_id: int,
    status: str,
) -> None:
    """Send a Postgres NOTIFY on the job's event channel for SSE listeners."""
    channel = f"job_events_{job_id.hex}"
    payload = json.dumps({"event_id": event_id, "status": status})
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": channel, "payload": payload},
    )


async def pause_user_jobs_for_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    key_type: str,
    reason: str,
) -> list[UUID]:
    """Pause queued/running jobs for `user_id` that require `key_type`."""
    stmt = (
        update(Job)
        .where(
            Job.user_id == user_id,
            Job.status.in_(["queued", "running"]),
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
            run_at=func.now(),
            updated_at=func.now(),
        )
        .returning(Job.id)
    )
    return list((await session.execute(stmt)).scalars().all())
