"""Initial schema: pgvector, users, auth, API keys, and decrypt audit log.

Revision ID: 0001
Revises:
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("addon_token", sa.Text(), nullable=False),
        sa.Column("llm_provider", sa.Text(), server_default=sa.text("'gemini'"), nullable=False),
        sa.Column("monthly_token_budget", sa.BigInteger(), nullable=True),
        sa.Column("jobs_paused_reason", sa.Text(), nullable=True),
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
            "llm_provider IN ('gemini', 'openai', 'anthropic', 'openrouter')",
            name="users_llm_provider_check",
        ),
        sa.CheckConstraint(
            "monthly_token_budget IS NULL OR monthly_token_budget > 0",
            name="users_monthly_token_budget_check",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("addon_token"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "login_tokens",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "token_hash"),
    )
    op.create_index("idx_login_tokens_hash", "login_tokens", ["token_hash"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "token_hash"),
    )
    op.create_index("idx_sessions_expires_at", "sessions", ["expires_at"], unique=False)
    op.create_index("idx_sessions_hash", "sessions", ["token_hash"], unique=True)

    op.create_table(
        "user_api_keys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key_type", sa.Text(), nullable=False),
        sa.Column("encrypted_key", sa.LargeBinary(), nullable=False),
        sa.Column("last4", sa.Text(), nullable=False),
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
            "key_type IN ('tmdb', 'gemini', 'openai', 'anthropic', 'openrouter')",
            name="user_api_keys_key_type_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "key_type"),
    )

    op.create_table(
        "key_decrypt_audit",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("key_type", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "key_type IN ('tmdb', 'gemini', 'openai', 'anthropic', 'openrouter')",
            name="key_decrypt_audit_key_type_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "id"),
    )
    op.create_index(
        "idx_key_decrypt_audit_user_created",
        "key_decrypt_audit",
        ["user_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_key_decrypt_audit_user_created", table_name="key_decrypt_audit")
    op.drop_table("key_decrypt_audit")
    op.drop_table("user_api_keys")
    op.drop_index("idx_sessions_hash", table_name="sessions")
    op.drop_index("idx_sessions_expires_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("idx_login_tokens_hash", table_name="login_tokens")
    op.drop_table("login_tokens")
    op.drop_table("users")
