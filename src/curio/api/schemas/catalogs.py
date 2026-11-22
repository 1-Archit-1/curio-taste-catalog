"""Catalog, preset, slot, candidate, and veto schemas for the control-plane API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

MediaType = Literal["movie", "series"]
VetoKind = Literal["seen", "not_for_me"]


def _strip_non_empty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be empty")
    return cleaned


class MessageResponse(BaseModel):
    message: str


class PresetCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    media_type: MediaType
    min_rating: float | None = Field(default=None, ge=0.0, le=10.0)
    criteria: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _strip_non_empty(value)


class PresetUpdateRequest(BaseModel):
    """Partial update: omitted fields are left unchanged; `null` clears a nullable field."""

    # Optional but not nullable. The default is never validated, so omitting `name` is
    # allowed while an explicit `null` fails `str` validation with a 422.
    name: str = Field(default=None, min_length=1)  # type: ignore[assignment]
    min_rating: float | None = Field(default=None, ge=0.0, le=10.0)
    criteria: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _strip_non_empty(value)


class PresetOut(BaseModel):
    id: UUID
    name: str
    media_type: MediaType
    min_rating: float | None = None
    criteria: str | None = None
    created_at: datetime
    updated_at: datetime


class PresetListResponse(BaseModel):
    presets: list[PresetOut]


class SlotAssignRequest(BaseModel):
    preset_id: UUID | None = None


class SlotOut(BaseModel):
    slot_id: str
    name: str
    media_type: MediaType
    preset_id: UUID | None = None
    preset_name: str | None = None


class SlotListResponse(BaseModel):
    slots: list[SlotOut]


class CandidateOut(BaseModel):
    media_type: MediaType
    tmdb_id: int
    imdb_id: str | None = None
    name: str
    year: int | None = None
    poster: str
    description: str | None = None
    rating: float | None = None
    blurb: str | None = None
    position: int
    watch_status: str | None = None
    veto_kind: VetoKind | None = None


class CandidateListResponse(BaseModel):
    preset_id: UUID
    candidates: list[CandidateOut]


class VetoRequest(BaseModel):
    kind: VetoKind


class VetoOut(BaseModel):
    media_type: MediaType
    tmdb_id: int
    kind: VetoKind
