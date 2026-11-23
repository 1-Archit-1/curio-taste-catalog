"""Background job, execution run, and progress event schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class JobOut(BaseModel):
    id: UUID
    preset_id: UUID | None = None
    job_kind: str
    status: str
    trigger: str
    paused_key_type: str | None = None
    paused_reason: str | None = None
    last_error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class RunOut(BaseModel):
    id: UUID
    pipeline: str
    trigger: str
    status: str
    latency_ms: int | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class RunEventOut(BaseModel):
    id: int
    step: str
    message: str
    payload: dict[str, Any] | None = None
    created_at: datetime


class JobStatusResponse(BaseModel):
    job: JobOut
    run: RunOut | None = None
    events: list[RunEventOut] = Field(default_factory=list)


class LatestPresetJobResponse(BaseModel):
    job: JobOut | None = None
    run: RunOut | None = None
    events: list[RunEventOut] = Field(default_factory=list)
