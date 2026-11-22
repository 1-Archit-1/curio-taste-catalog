"""Catalog, preset, slot, watch-history, and veto models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from curio.db.models import Base

_MEDIA_TYPE_CHECK = "media_type IN ('movie', 'series')"
_SLOT_ID_CHECK = (
    "slot_id IN ('curio_movie_1', 'curio_movie_2', 'curio_movie_3', "
    "'curio_series_1', 'curio_series_2', 'curio_series_3')"
)


class MediaItem(Base):
    """Shared catalog metadata for movies and series (no user_id).

    Keyed by TMDB `(media_type, tmdb_id)`: TMDB numbers movies and TV separately, and
    some TMDB titles have no IMDb ID. `imdb_id` is required only to serve a title to
    Stremio/Nuvio, which addresses items by `tt...` IDs.
    """

    __tablename__ = "media_items"
    __table_args__ = (
        CheckConstraint(_MEDIA_TYPE_CHECK, name="media_items_media_type_check"),
        CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 10)",
            name="media_items_rating_check",
        ),
    )

    media_type: Mapped[str] = mapped_column(Text, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    imdb_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    name: Mapped[str] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class Preset(Base):
    """Per-user catalog curation preset."""

    __tablename__ = "presets"
    __table_args__ = (
        CheckConstraint(_MEDIA_TYPE_CHECK, name="presets_media_type_check"),
        CheckConstraint(
            "min_rating IS NULL OR (min_rating >= 0 AND min_rating <= 10)",
            name="presets_min_rating_check",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    id: Mapped[UUID] = mapped_column(
        Uuid,
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text)
    min_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class Slot(Base):
    """Per-user fixed Stremio catalog slot."""

    __tablename__ = "slots"
    __table_args__ = (
        CheckConstraint(_SLOT_ID_CHECK, name="slots_slot_id_check"),
        CheckConstraint(_MEDIA_TYPE_CHECK, name="slots_media_type_check"),
        ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="SET NULL (preset_id)",
            name="fk_slots_user_preset",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    slot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    media_type: Mapped[str] = mapped_column(Text)
    preset_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CatalogCandidate(Base):
    """Per-user, per-preset ordered candidate media items."""

    __tablename__ = "catalog_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="CASCADE",
            name="fk_catalog_candidates_user_preset",
        ),
        ForeignKeyConstraint(
            ["media_type", "tmdb_id"],
            ["media_items.media_type", "media_items.tmdb_id"],
            ondelete="CASCADE",
            name="fk_catalog_candidates_media_item",
        ),
        Index(
            "idx_catalog_candidates_preset_pos",
            "user_id",
            "preset_id",
            "position",
            "media_type",
            "tmdb_id",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    preset_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    media_type: Mapped[str] = mapped_column(Text, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    blurb: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class WatchHistory(Base):
    """Per-user watch history and ratings."""

    __tablename__ = "watch_history"
    __table_args__ = (
        CheckConstraint(
            "status IN ('watched', 'dropped', 'watching')",
            name="watch_history_status_check",
        ),
        CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 10)",
            name="watch_history_rating_check",
        ),
        ForeignKeyConstraint(
            ["media_type", "tmdb_id"],
            ["media_items.media_type", "media_items.tmdb_id"],
            ondelete="CASCADE",
            name="fk_watch_history_media_item",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_type: Mapped[str] = mapped_column(Text, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'watched'"))
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(Text, server_default=text("'manual'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class Veto(Base):
    """Per-user explicit title vetoes (seen or not_for_me)."""

    __tablename__ = "vetoes"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('seen', 'not_for_me')",
            name="vetoes_kind_check",
        ),
        ForeignKeyConstraint(
            ["media_type", "tmdb_id"],
            ["media_items.media_type", "media_items.tmdb_id"],
            ondelete="CASCADE",
            name="fk_vetoes_media_item",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_type: Mapped[str] = mapped_column(Text, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
