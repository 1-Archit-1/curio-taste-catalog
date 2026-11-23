"""SQLAlchemy 2.0 declarative models — the single source of truth for the schema."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from curio.db.models.catalogs import (  # noqa: E402
    CatalogCandidate,
    MediaItem,
    Preset,
    ServedTitle,
    Slot,
    Veto,
    WatchHistory,
)
from curio.db.models.jobs import Job, Run, RunEvent  # noqa: E402
from curio.db.models.keys import KeyDecryptAudit, UserApiKey  # noqa: E402
from curio.db.models.sources import ImportIssue, Source, SourceItem, UserSource  # noqa: E402
from curio.db.models.users import LoginToken, Session, User  # noqa: E402

__all__ = [
    "Base",
    "CatalogCandidate",
    "ImportIssue",
    "Job",
    "KeyDecryptAudit",
    "LoginToken",
    "MediaItem",
    "Preset",
    "Run",
    "RunEvent",
    "ServedTitle",
    "Session",
    "Slot",
    "Source",
    "SourceItem",
    "User",
    "UserApiKey",
    "UserSource",
    "Veto",
    "WatchHistory",
]
