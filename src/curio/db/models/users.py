"""User, login token, and session models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from curio.db.models import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "llm_provider IN ('gemini', 'openai', 'anthropic', 'openrouter')",
            name="users_llm_provider_check",
        ),
        CheckConstraint(
            "monthly_token_budget IS NULL OR monthly_token_budget > 0",
            name="users_monthly_token_budget_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    username: Mapped[str] = mapped_column(Text, unique=True)
    addon_token: Mapped[str] = mapped_column(Text, unique=True)
    llm_provider: Mapped[str] = mapped_column(Text, server_default=text("'gemini'"))
    monthly_token_budget: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    jobs_paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class LoginToken(Base):
    __tablename__ = "login_tokens"
    __table_args__ = (Index("idx_login_tokens_hash", "token_hash", unique=True),)

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_hash", "token_hash", unique=True),
        Index("idx_sessions_expires_at", "expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
