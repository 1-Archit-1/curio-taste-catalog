"""Typed taste source and import-issue schemas for the control-plane API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from curio.api.schemas.catalogs import MediaType

# TMDB IDs are stored in a 32-bit integer column.
MAX_TMDB_ID = 2_147_483_647
SourceKind = Literal["lb_diary_rss", "lb_profile_csv", "lb_list_csv"]
SourceRole = Literal["self", "taste", "candidates"]
ImportItemKind = Literal["watched", "rating", "diary", "watchlist", "list"]
ImportIssueStatus = Literal["pending", "resolved", "dismissed"]


def _strip_non_empty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be empty")
    return cleaned


class SourceOut(BaseModel):
    id: UUID
    kind: SourceKind
    external_key: str
    label: str
    url: str | None = None
    role: SourceRole
    item_count: int
    # For an export source, when it was last imported.
    last_refreshed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class SourceListResponse(BaseModel):
    sources: list[SourceOut]


class SourcePreviewRequest(BaseModel):
    input: str = Field(min_length=1)

    @field_validator("input")
    @classmethod
    def _validate_input(cls, value: str) -> str:
        return _strip_non_empty(value)


class SourcePreviewItemOut(BaseModel):
    title: str
    year: int | None = None
    # Curio's 0-10 scale (Letterboxd stars x 2).
    rating: float | None = None
    watched_at: datetime | None = None
    tmdb_id: int | None = None


class SourcePreviewResponse(BaseModel):
    kind: SourceKind
    username: str
    label: str
    url: str
    entry_count: int
    recent_titles: list[SourcePreviewItemOut]


class SourceCreateRequest(BaseModel):
    input: str = Field(min_length=1)
    role: SourceRole = "self"

    @field_validator("input")
    @classmethod
    def _validate_input(cls, value: str) -> str:
        return _strip_non_empty(value)


class SourceUpdateRequest(BaseModel):
    role: SourceRole


class CandidateMatchOut(BaseModel):
    media_type: MediaType
    tmdb_id: int
    name: str
    year: int | None = None
    confidence: float


class ImportIssueOut(BaseModel):
    id: UUID
    source_id: UUID
    item_kind: ImportItemKind
    raw_title: str
    raw_year: int | None = None
    letterboxd_url: str | None = None
    # Curio's 0-10 scale (Letterboxd stars x 2).
    rating: float | None = None
    watched_at: datetime | None = None
    confidence: float | None = None
    candidate_matches: list[CandidateMatchOut]
    status: ImportIssueStatus
    resolved_media_type: MediaType | None = None
    resolved_tmdb_id: int | None = None
    created_at: datetime
    updated_at: datetime


class ImportIssueListResponse(BaseModel):
    issues: list[ImportIssueOut]
    pending_count: int


class ImportIssueResolveRequest(BaseModel):
    media_type: MediaType = "movie"
    tmdb_id: int = Field(ge=1, le=MAX_TMDB_ID)
