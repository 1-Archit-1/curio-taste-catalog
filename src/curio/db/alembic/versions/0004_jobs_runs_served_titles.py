"""Background jobs, runs, run events, served titles, and API key pause state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_api_keys", sa.Column("paused_reason", sa.Text(), nullable=True))
    op.add_column(
        "user_api_keys",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=True),
        sa.Column("job_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("trigger", sa.Text(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column(
            "required_keys",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("paused_key_type", sa.Text(), nullable=True),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "job_kind IN ('regenerate', 'compile')",
            name="jobs_job_kind_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')",
            name="jobs_status_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="CASCADE",
            name="fk_jobs_preset",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_jobs_active_preset_kind",
        "jobs",
        ["user_id", "preset_id", "job_kind"],
        unique=True,
        postgresql_where=sa.text(
            "preset_id IS NOT NULL AND status IN ('queued', 'running', 'paused')"
        ),
    )
    op.create_index(
        "idx_jobs_claimable",
        "jobs",
        ["run_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "idx_jobs_lease_expiry",
        "jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("idx_jobs_user_status", "jobs", ["user_id", "status"], unique=False)
    op.create_index(
        "idx_jobs_user_preset_created",
        "jobs",
        ["user_id", "preset_id", sa.literal_column("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'paused', 'succeeded', 'failed')",
            name="runs_status_check",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="CASCADE",
            name="fk_runs_preset",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_runs_user_started",
        "runs",
        ["user_id", sa.literal_column("started_at DESC")],
        unique=False,
    )
    op.create_index(
        "idx_runs_job_started",
        "runs",
        ["job_id", sa.literal_column("started_at DESC")],
        unique=False,
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_run_events_run_id", "run_events", ["run_id", "id"], unique=False)
    op.create_index("idx_run_events_user_id", "run_events", ["user_id"], unique=False)

    op.create_table(
        "served_titles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column(
            "first_served_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_served_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("serve_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "preset_id"],
            ["presets.user_id", "presets.id"],
            ondelete="CASCADE",
            name="fk_served_titles_preset",
        ),
        sa.PrimaryKeyConstraint("user_id", "preset_id", "media_type", "tmdb_id"),
    )


def downgrade() -> None:
    op.drop_table("served_titles")
    op.drop_index("idx_run_events_user_id", table_name="run_events")
    op.drop_index("idx_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("idx_runs_job_started", table_name="runs")
    op.drop_index("idx_runs_user_started", table_name="runs")
    op.drop_table("runs")
    op.drop_index("idx_jobs_user_preset_created", table_name="jobs")
    op.drop_index("idx_jobs_user_status", table_name="jobs")
    op.drop_index("idx_jobs_lease_expiry", table_name="jobs")
    op.drop_index("idx_jobs_claimable", table_name="jobs")
    op.drop_index("uq_jobs_active_preset_kind", table_name="jobs")
    op.drop_table("jobs")
    op.drop_column("user_api_keys", "paused_at")
    op.drop_column("user_api_keys", "paused_reason")
