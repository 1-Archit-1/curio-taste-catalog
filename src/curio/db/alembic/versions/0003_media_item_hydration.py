"""Hydrated TMDB metadata and embedding on the shared media cache.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_items", sa.Column("poster_path", sa.Text(), nullable=True))
    for name in ("genres", "keywords", "cast_names", "directors"):
        op.add_column(
            "media_items",
            sa.Column(
                name,
                sa.ARRAY(sa.Text()),
                server_default=sa.text("'{}'::text[]"),
                nullable=False,
            ),
        )
    for name in ("genre_ids", "keyword_ids"):
        op.add_column(
            "media_items",
            sa.Column(
                name,
                sa.ARRAY(sa.Integer()),
                server_default=sa.text("'{}'::integer[]"),
                nullable=False,
            ),
        )
    op.add_column("media_items", sa.Column("vote_count", sa.Integer(), nullable=True))
    op.add_column("media_items", sa.Column("popularity", sa.Float(), nullable=True))
    op.add_column("media_items", sa.Column("original_language", sa.Text(), nullable=True))
    op.add_column("media_items", sa.Column("runtime", sa.Integer(), nullable=True))
    op.add_column("media_items", sa.Column("release_date", sa.Date(), nullable=True))
    op.add_column(
        "media_items", sa.Column("hydrated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "media_items",
        sa.Column("embedding", pgvector.sqlalchemy.Vector(768), nullable=True),
    )


def downgrade() -> None:
    for name in (
        "embedding",
        "hydrated_at",
        "release_date",
        "runtime",
        "original_language",
        "popularity",
        "vote_count",
        "keyword_ids",
        "genre_ids",
        "directors",
        "cast_names",
        "keywords",
        "genres",
        "poster_path",
    ):
        op.drop_column("media_items", name)
