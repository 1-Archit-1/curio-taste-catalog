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
        "directors",
        "cast_names",
        "keywords",
        "genres",
        "poster_path",
    ):
        op.drop_column("media_items", name)
