"""Database persistence for typed taste sources, source items, and import issues."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.catalogs import MediaItem, WatchHistory
from curio.db.models.sources import ImportIssue, Source, SourceItem, UserSource

_WATCH_SIGNAL_KINDS = ("watched", "rating", "diary")


async def get_source_by_id(session: AsyncSession, source_id: UUID) -> Source | None:
    return await session.scalar(select(Source).where(Source.id == source_id))


async def get_or_create_source(
    session: AsyncSession,
    *,
    kind: str,
    external_key: str,
    label: str,
    url: str | None,
) -> Source:
    stmt = pg_insert(Source).values(
        kind=kind,
        external_key=external_key,
        label=label,
        url=url,
    )
    stmt = (
        stmt.on_conflict_do_update(
            index_elements=["kind", "external_key"],
            set_={
                "label": label,
                "url": func.coalesce(stmt.excluded.url, Source.url),
                "updated_at": func.now(),
            },
        )
        .returning(Source)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def update_source_refresh_state(
    session: AsyncSession,
    *,
    source_id: UUID,
    item_count: int,
    last_refreshed_at: datetime | None = None,
    last_error: str | None = None,
) -> Source:
    values: dict[str, object] = {
        "item_count": item_count,
        "last_error": last_error,
        "updated_at": func.now(),
    }
    if last_refreshed_at is not None:
        values["last_refreshed_at"] = last_refreshed_at
    stmt = (
        update(Source)
        .where(Source.id == source_id)
        .values(**values)
        .returning(Source)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def count_source_items(session: AsyncSession, *, source_id: UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(SourceItem).where(SourceItem.source_id == source_id)
    )
    return int(count or 0)


async def list_rss_sources(session: AsyncSession) -> list[Source]:
    rows = await session.scalars(
        select(Source)
        .where(Source.kind == "lb_diary_rss")
        .order_by(Source.last_refreshed_at.asc().nullsfirst(), Source.created_at.asc())
    )
    return list(rows)


async def list_user_sources(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[tuple[Source, UserSource]]:
    rows = await session.execute(
        select(Source, UserSource)
        .join(UserSource, UserSource.source_id == Source.id)
        .where(UserSource.user_id == user_id)
        .order_by(UserSource.created_at.desc(), Source.label.asc())
    )
    return [(row[0], row[1]) for row in rows.all()]


async def get_user_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
) -> tuple[Source, UserSource] | None:
    row = (
        await session.execute(
            select(Source, UserSource)
            .join(UserSource, UserSource.source_id == Source.id)
            .where(UserSource.user_id == user_id, UserSource.source_id == source_id)
        )
    ).first()
    if row is None:
        return None
    return (row[0], row[1])


async def upsert_user_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
    role: str,
) -> UserSource:
    stmt = (
        pg_insert(UserSource)
        .values(user_id=user_id, source_id=source_id, role=role)
        .on_conflict_do_update(
            index_elements=["user_id", "source_id"],
            set_={"role": role, "updated_at": func.now()},
        )
        .returning(UserSource)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def update_user_source_role(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
    role: str,
) -> UserSource | None:
    stmt = (
        update(UserSource)
        .where(UserSource.user_id == user_id, UserSource.source_id == source_id)
        .values(role=role, updated_at=func.now())
        .returning(UserSource)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def delete_user_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
) -> bool:
    result = await session.execute(
        delete(UserSource).where(
            UserSource.user_id == user_id,
            UserSource.source_id == source_id,
        )
    )
    return (result.rowcount or 0) > 0


async def list_self_user_ids_for_source(
    session: AsyncSession,
    *,
    source_id: UUID,
) -> list[UUID]:
    rows = await session.scalars(
        select(UserSource.user_id).where(
            UserSource.source_id == source_id,
            UserSource.role == "self",
        )
    )
    return list(rows)


async def ensure_media_item_stubs(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    """Insert unhydrated MediaItem stubs if they do not exist yet."""
    if not rows:
        return
    stmt = (
        pg_insert(MediaItem)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["media_type", "tmdb_id"])
    )
    await session.execute(stmt)


async def upsert_source_items(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    stmt = pg_insert(SourceItem).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_id", "media_type", "tmdb_id", "item_kind"],
        set_={
            "rating": func.coalesce(stmt.excluded.rating, SourceItem.rating),
            "watched_at": func.coalesce(stmt.excluded.watched_at, SourceItem.watched_at),
            "entry_url": func.coalesce(stmt.excluded.entry_url, SourceItem.entry_url),
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def list_source_watch_signals(
    session: AsyncSession,
    *,
    source_id: UUID,
) -> list[SourceItem]:
    rows = await session.scalars(
        select(SourceItem).where(
            SourceItem.source_id == source_id,
            SourceItem.item_kind.in_(_WATCH_SIGNAL_KINDS),
        )
    )
    return list(rows)


async def upsert_watch_history_from_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    rows: list[dict[str, object]],
) -> None:
    """Upsert watch_history rows for `user_id` without overwriting existing non-null fields."""
    if not rows:
        return
    payload = [{**r, "user_id": user_id} for r in rows]
    stmt = pg_insert(WatchHistory).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "media_type", "tmdb_id"],
        set_={
            "rating": func.coalesce(stmt.excluded.rating, WatchHistory.rating),
            "watched_at": func.coalesce(stmt.excluded.watched_at, WatchHistory.watched_at),
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def upsert_import_issue(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
    item_kind: str,
    raw_title: str,
    raw_year: int | None,
    letterboxd_url: str | None,
    rating: float | None,
    watched_at: datetime | None,
    confidence: float | None,
    candidate_matches: list[dict[str, Any]],
) -> tuple[ImportIssue, bool]:
    """Create or update a pending import issue. Returns `(issue, is_active_pending)`."""
    year_cond = (
        ImportIssue.raw_year.is_(None) if raw_year is None else ImportIssue.raw_year == raw_year
    )
    existing = await session.scalar(
        select(ImportIssue).where(
            ImportIssue.user_id == user_id,
            ImportIssue.source_id == source_id,
            ImportIssue.item_kind == item_kind,
            ImportIssue.raw_title == raw_title,
            year_cond,
        )
    )
    if existing is not None:
        if existing.status != "pending":
            return (existing, False)
        existing.letterboxd_url = letterboxd_url or existing.letterboxd_url
        existing.rating = rating if rating is not None else existing.rating
        existing.watched_at = watched_at if watched_at is not None else existing.watched_at
        existing.confidence = confidence
        existing.candidate_matches = candidate_matches
        existing.updated_at = datetime.now().astimezone()
        await session.flush()
        return (existing, True)

    issue = ImportIssue(
        user_id=user_id,
        source_id=source_id,
        item_kind=item_kind,
        raw_title=raw_title,
        raw_year=raw_year,
        letterboxd_url=letterboxd_url,
        rating=rating,
        watched_at=watched_at,
        confidence=confidence,
        candidate_matches=candidate_matches,
        status="pending",
    )
    session.add(issue)
    await session.flush()
    return (issue, True)


async def list_import_issues(
    session: AsyncSession,
    *,
    user_id: UUID,
    status: str | None = None,
) -> list[ImportIssue]:
    stmt = select(ImportIssue).where(ImportIssue.user_id == user_id)
    if status is not None:
        stmt = stmt.where(ImportIssue.status == status)
    stmt = stmt.order_by(ImportIssue.created_at.desc(), ImportIssue.raw_title.asc())
    return list(await session.scalars(stmt))


async def count_pending_import_issues(session: AsyncSession, *, user_id: UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(ImportIssue)
        .where(ImportIssue.user_id == user_id, ImportIssue.status == "pending")
    )
    return int(count or 0)


async def get_import_issue(
    session: AsyncSession,
    *,
    user_id: UUID,
    issue_id: UUID,
) -> ImportIssue | None:
    return await session.scalar(
        select(ImportIssue).where(
            ImportIssue.user_id == user_id,
            ImportIssue.id == issue_id,
        )
    )


async def resolve_import_issue(
    session: AsyncSession,
    *,
    user_id: UUID,
    issue_id: UUID,
    media_type: str,
    tmdb_id: int,
) -> ImportIssue | None:
    stmt = (
        update(ImportIssue)
        .where(ImportIssue.user_id == user_id, ImportIssue.id == issue_id)
        .values(
            status="resolved",
            resolved_media_type=media_type,
            resolved_tmdb_id=tmdb_id,
            updated_at=func.now(),
        )
        .returning(ImportIssue)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def dismiss_import_issue(
    session: AsyncSession,
    *,
    user_id: UUID,
    issue_id: UUID,
) -> ImportIssue | None:
    stmt = (
        update(ImportIssue)
        .where(ImportIssue.user_id == user_id, ImportIssue.id == issue_id)
        .values(status="dismissed", updated_at=func.now())
        .returning(ImportIssue)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)
