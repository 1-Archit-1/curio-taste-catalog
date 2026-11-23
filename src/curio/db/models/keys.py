"""Encrypted per-user API key and decrypt audit log models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    Text,
    Uuid,
    desc,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from curio.db.models import Base

_KEY_TYPE_CHECK = "key_type IN ('tmdb', 'gemini', 'openai', 'anthropic', 'openrouter')"


class UserApiKey(Base):
    __tablename__ = "user_api_keys"
    __table_args__ = (CheckConstraint(_KEY_TYPE_CHECK, name="user_api_keys_key_type_check"),)

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key_type: Mapped[str] = mapped_column(Text, primary_key=True)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary)
    last4: Mapped[str] = mapped_column(Text)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class KeyDecryptAudit(Base):
    __tablename__ = "key_decrypt_audit"
    __table_args__ = (
        CheckConstraint(_KEY_TYPE_CHECK, name="key_decrypt_audit_key_type_check"),
        Index("idx_key_decrypt_audit_user_created", "user_id", desc("created_at"), desc("id")),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    key_type: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
