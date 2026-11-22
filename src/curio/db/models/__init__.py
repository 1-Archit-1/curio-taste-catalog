"""SQLAlchemy 2.0 declarative models — the single source of truth for the schema."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from curio.db.models.catalogs import (  # noqa: E402
    CatalogCandidate,
    MediaItem,
    Preset,
    Slot,
    Veto,
    WatchHistory,
)
from curio.db.models.keys import KeyDecryptAudit, UserApiKey  # noqa: E402
from curio.db.models.users import LoginToken, Session, User  # noqa: E402

__all__ = [
    "Base",
    "CatalogCandidate",
    "KeyDecryptAudit",
    "LoginToken",
    "MediaItem",
    "Preset",
    "Session",
    "Slot",
    "User",
    "UserApiKey",
    "Veto",
    "WatchHistory",
]
