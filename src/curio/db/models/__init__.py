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
from curio.db.models.simkl import (  # noqa: E402
    SimklConnection,
    SimklOAuthState,
    SimklRevocation,
)
from curio.db.models.sources import (  # noqa: E402
    ImportIssue,
    Source,
    SourceItem,
    SourceUpload,
    UserSource,
)
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
    "SimklConnection",
    "SimklOAuthState",
    "SimklRevocation",
    "Slot",
    "Source",
    "SourceItem",
    "SourceUpload",
    "User",
    "UserApiKey",
    "UserSource",
    "Veto",
    "WatchHistory",
]
