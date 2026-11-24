"""Typed taste sources, source items, uploads, import issues, and watch history provenance.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_refresh_at", sa.DateTime(timezone=True), nullable=True),
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
            "kind IN ('lb_diary_rss', 'lb_profile_csv', 'lb_list_csv')",
            name="sources_kind_check",
        ),
        sa.CheckConstraint(
            "(kind = 'lb_diary_rss') = (owner_user_id IS NULL)",
            name="sources_owner_kind_check",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind",
            "owner_user_id",
            "external_key",
            name="uq_sources_kind_owner_external_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "user_sources",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
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
            "role IN ('self', 'taste', 'candidates')",
            name="user_sources_role_check",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "source_id"),
    )
    op.create_index("idx_user_sources_source_id", "user_sources", ["source_id"], unique=False)

    op.create_table(
        "source_items",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_url", sa.Text(), nullable=True),
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
            "media_type IN ('movie', 'series')", name="source_items_media_type_check"
        ),
        sa.CheckConstraint(
            "item_kind IN ('watched', 'rating', 'diary', 'watchlist', 'list')",
            name="source_items_item_kind_check",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 10)",
            name="source_items_rating_check",
        ),
        sa.ForeignKeyConstraint(
            ["media_type", "tmdb_id"],
            ["media_items.media_type", "media_items.tmdb_id"],
            ondelete="CASCADE",
            name="fk_source_items_media_item",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id", "media_type", "tmdb_id", "item_kind"),
    )

    op.create_table(
        "import_issues",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("raw_title", sa.Text(), nullable=False),
        sa.Column("raw_year", sa.Integer(), nullable=True),
        sa.Column("letterboxd_url", sa.Text(), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "candidate_matches",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("resolved_media_type", sa.Text(), nullable=True),
        sa.Column("resolved_tmdb_id", sa.Integer(), nullable=True),
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
            "item_kind IN ('watched', 'rating', 'diary', 'watchlist', 'list')",
            name="import_issues_item_kind_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name="import_issues_status_check",
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 10)",
            name="import_issues_rating_check",
        ),
        sa.CheckConstraint(
            "resolved_media_type IS NULL OR resolved_media_type IN ('movie', 'series')",
            name="import_issues_resolved_media_type_check",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "id"),
    )
    op.create_index(
        "idx_import_issues_user_status", "import_issues", ["user_id", "status"], unique=False
    )
    op.create_index(
        "idx_import_issues_user_source", "import_issues", ["user_id", "source_id"], unique=False
    )
    op.create_table(
        "source_uploads",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_source_uploads_user_id", "source_uploads", ["user_id"], unique=False)
    # Rows a source wrote carry its id, so they go away with it (ADR 0009).
    op.add_column("watch_history", sa.Column("source_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "watch_history_source_id_fkey",
        "watch_history",
        "sources",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_watch_history_source_id",
        "watch_history",
        ["source_id"],
        unique=False,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.drop_constraint("jobs_job_kind_check", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_job_kind_check", "jobs", "job_kind IN ('regenerate', 'compile', 'lb_import')"
    )


def downgrade() -> None:
    # `lb_import` jobs would violate the restored constraint; their uploads go with the table.
    op.execute("DELETE FROM jobs WHERE job_kind = 'lb_import'")
    op.drop_constraint("jobs_job_kind_check", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_job_kind_check", "jobs", "job_kind IN ('regenerate', 'compile')"
    )
    op.drop_index("idx_watch_history_source_id", table_name="watch_history")
    op.drop_constraint("watch_history_source_id_fkey", "watch_history", type_="foreignkey")
    op.drop_column("watch_history", "source_id")
    op.drop_index("idx_source_uploads_user_id", table_name="source_uploads")
    op.drop_table("source_uploads")
    op.drop_index("idx_import_issues_user_source", table_name="import_issues")
    op.drop_index("idx_import_issues_user_status", table_name="import_issues")
    op.drop_table("import_issues")
    op.drop_table("source_items")
    op.drop_index("idx_user_sources_source_id", table_name="user_sources")
    op.drop_table("user_sources")
    op.drop_table("sources")
