"""Typed taste source, source-item, upload, and import-issue models (ADR 0003, 0004, 0009)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from curio.db.models import Base

_SOURCE_KIND_CHECK = "kind IN ('lb_diary_rss', 'lb_profile_csv', 'lb_list_csv')"
# Public kinds are shared and have no owner; every other kind belongs to the user who uploaded it.
_SOURCE_OWNER_CHECK = "(kind = 'lb_diary_rss') = (owner_user_id IS NULL)"
_USER_SOURCE_ROLE_CHECK = "role IN ('self', 'taste', 'candidates')"
_ITEM_KIND_CHECK = "item_kind IN ('watched', 'rating', 'diary', 'watchlist', 'list')"
_MEDIA_TYPE_CHECK = "media_type IN ('movie', 'series')"
_ISSUE_STATUS_CHECK = "status IN ('pending', 'resolved', 'dismissed')"
# Ratings use Curio's 0-10 scale (Letterboxd stars x 2) in every table.
_RATING_CHECK = "rating IS NULL OR (rating >= 0 AND rating <= 10)"


class Source(Base):
    """Taste source. Public kinds are shared; export kinds belong to `owner_user_id` (ADR 0009)."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(_SOURCE_KIND_CHECK, name="sources_kind_check"),
        CheckConstraint(_SOURCE_OWNER_CHECK, name="sources_owner_kind_check"),
        UniqueConstraint(
            "kind",
            "owner_user_id",
            "external_key",
            name="uq_sources_kind_owner_external_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    external_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the poller may fetch this public source next. NULL means as soon as possible.
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )


class UserSource(Base):
    """Per-user subscription to a shared source with a typed role."""

    __tablename__ = "user_sources"
    __table_args__ = (
        CheckConstraint(_USER_SOURCE_ROLE_CHECK, name="user_sources_role_check"),
        Index("idx_user_sources_source_id", "source_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )


class SourceItem(Base):
    """Shared normalized title signals belonging to a source."""

    __tablename__ = "source_items"
    __table_args__ = (
        CheckConstraint(_MEDIA_TYPE_CHECK, name="source_items_media_type_check"),
        CheckConstraint(_ITEM_KIND_CHECK, name="source_items_item_kind_check"),
        CheckConstraint(_RATING_CHECK, name="source_items_rating_check"),
        ForeignKeyConstraint(
            ["media_type", "tmdb_id"],
            ["media_items.media_type", "media_items.tmdb_id"],
            ondelete="CASCADE",
            name="fk_source_items_media_item",
        ),
    )

    source_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_type: Mapped[str] = mapped_column(Text, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )


class ImportIssue(Base):
    """Per-user unresolved or low-confidence export rows awaiting review."""

    __tablename__ = "import_issues"
    __table_args__ = (
        CheckConstraint(_ITEM_KIND_CHECK, name="import_issues_item_kind_check"),
        CheckConstraint(_ISSUE_STATUS_CHECK, name="import_issues_status_check"),
        CheckConstraint(_RATING_CHECK, name="import_issues_rating_check"),
        CheckConstraint(
            "resolved_media_type IS NULL OR resolved_media_type IN ('movie', 'series')",
            name="import_issues_resolved_media_type_check",
        ),
        Index("idx_import_issues_user_status", "user_id", "status"),
        Index("idx_import_issues_user_source", "user_id", "source_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_kind: Mapped[str] = mapped_column(Text, nullable=False)
    raw_title: Mapped[str] = mapped_column(Text, nullable=False)
    raw_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    letterboxd_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_matches: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text,
        server_default=text("'pending'"),
        nullable=False,
    )
    resolved_media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )


class SourceUpload(Base):
    """An uploaded export ZIP waiting for its `lb_import` job. Deleted when the job ends."""

    __tablename__ = "source_uploads"
    __table_args__ = (Index("idx_source_uploads_user_id", "user_id"),)

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
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
