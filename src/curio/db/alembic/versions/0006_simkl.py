"""Simkl: OAuth states, connections, revocations, and the `simkl` source kind.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SOURCE_KINDS = "kind IN ('lb_diary_rss', 'lb_profile_csv', 'lb_list_csv')"
_NEW_SOURCE_KINDS = "kind IN ('lb_diary_rss', 'lb_profile_csv', 'lb_list_csv', 'simkl')"
_OLD_ITEM_KINDS = "item_kind IN ('watched', 'rating', 'diary', 'watchlist', 'list')"
_NEW_ITEM_KINDS = (
    "item_kind IN ('watched', 'rating', 'diary', 'watchlist', 'list', 'watching', 'dropped')"
)
_OLD_JOB_KINDS = "job_kind IN ('regenerate', 'compile', 'lb_import')"
_NEW_JOB_KINDS = "job_kind IN ('regenerate', 'compile', 'lb_import', 'simkl_sync', 'simkl_revoke')"
_OLD_AUDIT_KEY_TYPES = "key_type IN ('tmdb', 'gemini', 'openai', 'anthropic', 'openrouter')"
_NEW_AUDIT_KEY_TYPES = (
    "key_type IN ('tmdb', 'gemini', 'openai', 'anthropic', 'openrouter', 'simkl')"
)


def _swap_check(table: str, name: str, expression: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expression)


def upgrade() -> None:
    # `sources_owner_kind_check` already requires an owner for every kind but `lb_diary_rss`,
    # so a `simkl` source can't exist without one.
    _swap_check("sources", "sources_kind_check", _NEW_SOURCE_KINDS)
    _swap_check("source_items", "source_items_item_kind_check", _NEW_ITEM_KINDS)
    _swap_check("jobs", "jobs_job_kind_check", _NEW_JOB_KINDS)
    _swap_check("key_decrypt_audit", "key_decrypt_audit_key_type_check", _NEW_AUDIT_KEY_TYPES)

    op.create_table(
        "simkl_oauth_states",
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_hash", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("state_hash"),
    )
    op.create_index("idx_simkl_oauth_states_user_id", "simkl_oauth_states", ["user_id"])
    op.create_index("idx_simkl_oauth_states_expires_at", "simkl_oauth_states", ["expires_at"])

    op.create_table(
        "simkl_connections",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("access_token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("simkl_account_id", sa.BigInteger(), nullable=True),
        sa.Column("simkl_account_name", sa.Text(), nullable=True),
        sa.Column("needs_reconnect", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("activities_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "idx_simkl_connections_source_id", "simkl_connections", ["source_id"], unique=True
    )

    op.create_table(
        "simkl_revocations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_simkl_revocations_user_id", "simkl_revocations", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_simkl_revocations_user_id", table_name="simkl_revocations")
    op.drop_table("simkl_revocations")
    op.drop_index("idx_simkl_connections_source_id", table_name="simkl_connections")
    op.drop_table("simkl_connections")
    op.drop_index("idx_simkl_oauth_states_expires_at", table_name="simkl_oauth_states")
    op.drop_index("idx_simkl_oauth_states_user_id", table_name="simkl_oauth_states")
    op.drop_table("simkl_oauth_states")

    # Rows that use the new values would violate the restored constraints. Deleting a simkl
    # source cascades to its items, subscriptions and the watch history it wrote. The
    # `watching` and `dropped` item kinds are only ever written by Simkl sources, so the second
    # statement is a safeguard.
    op.execute("DELETE FROM sources WHERE kind = 'simkl'")
    op.execute("DELETE FROM source_items WHERE item_kind IN ('watching', 'dropped')")
    op.execute("DELETE FROM jobs WHERE job_kind IN ('simkl_sync', 'simkl_revoke')")
    op.execute("DELETE FROM key_decrypt_audit WHERE key_type = 'simkl'")
    _swap_check("key_decrypt_audit", "key_decrypt_audit_key_type_check", _OLD_AUDIT_KEY_TYPES)
    _swap_check("jobs", "jobs_job_kind_check", _OLD_JOB_KINDS)
    _swap_check("source_items", "source_items_item_kind_check", _OLD_ITEM_KINDS)
    _swap_check("sources", "sources_kind_check", _OLD_SOURCE_KINDS)
