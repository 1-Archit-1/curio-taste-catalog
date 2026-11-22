"""Catalog, preset, slot, candidate, and veto schemas for the control-plane API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    name: str | None = Field(default=None, min_length=1)
    min_rating: float | None = Field(default=None, ge=0.0, le=10.0)
    criteria: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("must not be empty")
        return _strip_non_empty(value)


class PresetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    imdb_id: str
    media_type: MediaType
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
    model_config = ConfigDict(from_attributes=True)

    imdb_id: str
    kind: VetoKind
