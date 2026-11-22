"""Addon catalogs, presets, slots, watch history, vetoes, and taste views.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_items",
        sa.Column("imdb_id", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_url", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "media_type IN ('movie', 'series')",
            name="media_items_media_type_check",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 10)",
            name="media_items_rating_check",
        ),
        sa.PrimaryKeyConstraint("imdb_id"),
    )

    op.create_table(
        "presets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("min_rating", sa.Float(), nullable=True),
        sa.Column("criteria", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "media_type IN ('movie', 'series')",
            name="presets_media_type_check",
        ),
        sa.CheckConstraint(
            "min_rating IS NULL OR (min_rating >= 0 AND min_rating <= 10)",
            name="presets_min_rating_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "id"),
    )

    op.create_table(
        "slots",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slot_id IN ('curio_movie_1', 'curio_movie_2', 'curio_movie_3', "
            "'curio_series_1', 'curio_series_2', 'curio_series_3')",
            name="slots_slot_id_check",
        ),
        sa.CheckConstraint(
            "media_type IN ('movie', 'series')",
            name="slots_media_type_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="SET NULL",
            name="fk_slots_user_preset",
        ),
        sa.PrimaryKeyConstraint("user_id", "slot_id"),
    )

    op.create_table(
        "catalog_candidates",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=False),
        sa.Column("imdb_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blurb", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["imdb_id"], ["media_items.imdb_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="CASCADE",
            name="fk_catalog_candidates_user_preset",
        ),
        sa.PrimaryKeyConstraint("user_id", "preset_id", "imdb_id"),
    )
    op.create_index(
        "idx_catalog_candidates_preset_pos",
        "catalog_candidates",
        ["user_id", "preset_id", "position", "imdb_id"],
        unique=False,
    )

    op.create_table(
        "watch_history",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("imdb_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'watched'"), nullable=False),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.Text(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('watched', 'dropped', 'watching')",
            name="watch_history_status_check",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 10)",
            name="watch_history_rating_check",
        ),
        sa.ForeignKeyConstraint(["imdb_id"], ["media_items.imdb_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "imdb_id"),
    )

    op.create_table(
        "vetoes",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("imdb_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('seen', 'not_for_me')",
            name="vetoes_kind_check",
        ),
        sa.ForeignKeyConstraint(["imdb_id"], ["media_items.imdb_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "imdb_id"),
    )

    op.execute(
        """
        CREATE VIEW excluded_titles AS
        SELECT user_id, imdb_id, 'watch_history'::text AS reason
        FROM watch_history
        WHERE status IN ('watched', 'dropped')
        UNION
        SELECT user_id, imdb_id, kind AS reason
        FROM vetoes
        """
    )

    op.execute(
        """
        CREATE VIEW anti_taste AS
        SELECT w.user_id, w.imdb_id, 'low_rating'::text AS reason, w.rating
        FROM watch_history w
        WHERE w.rating IS NOT NULL AND w.rating <= 5.0
        UNION ALL
        SELECT w.user_id, w.imdb_id, 'dropped_series'::text AS reason, w.rating
        FROM watch_history w
        JOIN media_items m ON m.imdb_id = w.imdb_id
        WHERE w.status = 'dropped' AND m.media_type = 'series'
        UNION ALL
        SELECT v.user_id, v.imdb_id, 'not_for_me'::text AS reason, NULL::double precision AS rating
        FROM vetoes v
        WHERE v.kind = 'not_for_me'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS anti_taste")
    op.execute("DROP VIEW IF EXISTS excluded_titles")
    op.drop_table("vetoes")
    op.drop_table("watch_history")
    op.drop_index("idx_catalog_candidates_preset_pos", table_name="catalog_candidates")
    op.drop_table("catalog_candidates")
    op.drop_table("slots")
    op.drop_table("presets")
    op.drop_table("media_items")
