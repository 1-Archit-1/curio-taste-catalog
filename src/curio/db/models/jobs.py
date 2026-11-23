"""Background job queue, execution run, and run event models."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
    desc,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from curio.db.models import Base

_JOB_KIND_CHECK = "job_kind IN ('regenerate', 'compile')"
_JOB_STATUS_CHECK = "status IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')"
_RUN_STATUS_CHECK = "status IN ('running', 'paused', 'succeeded', 'failed')"


class Job(Base):
    """Per-user background job queue entry."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_JOB_KIND_CHECK, name="jobs_job_kind_check"),
        CheckConstraint(_JOB_STATUS_CHECK, name="jobs_status_check"),
        ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="CASCADE",
            name="fk_jobs_preset",
        ),
        Index(
            "uq_jobs_active_preset_kind",
            "user_id",
            "preset_id",
            "job_kind",
            unique=True,
            postgresql_where=text(
                "preset_id IS NOT NULL AND status IN ('queued', 'running', 'paused')"
            ),
        ),
        Index(
            "idx_jobs_claimable",
            "run_at",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("idx_jobs_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    preset_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    job_kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'queued'"),
    )
    trigger: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'manual'"),
    )
    required_keys: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    )
    paused_key_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("3"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class Run(Base):
    """Execution record for a single job attempt."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_RUN_STATUS_CHECK, name="runs_status_check"),
        ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="CASCADE",
            name="fk_runs_preset",
        ),
        Index("idx_runs_user_started", "user_id", desc("started_at")),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("jobs.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    preset_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    pipeline: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Base):
    """Structured step-by-step progress event emitted during a run."""

    __tablename__ = "run_events"
    __table_args__ = (Index("idx_run_events_run_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    step: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
