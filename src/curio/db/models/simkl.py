"""Simkl OAuth state, connection, and revocation models (#6, ADR 0009, ADR 0010).

Tokens are sealed boxes (ADR 0002): the API writes them with the public key and only workers can
read them. No table here holds a plaintext token.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from curio.db.models import Base


class SimklOAuthState(Base):
    """One pending authorization: the `state` and PKCE verifier of a started connect flow.

    The `state` value is stored as a hash and bound to the user and the browser session that
    started the flow. The callback consumes the row with a single `DELETE ... RETURNING`, so a
    state works once, whatever the outcome.
    """

    __tablename__ = "simkl_oauth_states"
    __table_args__ = (
        Index("idx_simkl_oauth_states_user_id", "user_id"),
        Index("idx_simkl_oauth_states_expires_at", "expires_at"),
    )

    state_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # `sessions.token_hash` of the browser session that started the flow.
    session_hash: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SimklConnection(Base):
    """A user's Simkl grant and sync state. One per user."""

    __tablename__ = "simkl_connections"
    __table_args__ = (Index("idx_simkl_connections_source_id", "source_id", unique=True),)

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    access_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    simkl_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    simkl_account_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_reconnect: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The whole `/sync/activities` response of the last complete sync, for `date_from` and for
    # telling which buckets moved. Saved in the transaction that commits the items.
    activities_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )


class SimklRevocation(Base):
    """A grant waiting to be revoked at Simkl by a worker (`simkl_revoke` job).

    Disconnecting or reconnecting drops the connection row at once, but only a worker can read
    the token. The sealed refresh token moves here. Revoking either token of a grant revokes
    both, so the access token is not kept.
    """

    __tablename__ = "simkl_revocations"
    __table_args__ = (Index("idx_simkl_revocations_user_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    refresh_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
