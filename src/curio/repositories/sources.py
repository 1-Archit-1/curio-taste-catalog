"""Database persistence for typed taste sources, source items, and import issues."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.catalogs import MediaItem, WatchHistory
from curio.db.models.sources import ImportIssue, Source, SourceItem, SourceUpload, UserSource

_WATCH_SIGNAL_KINDS = ("watched", "rating", "diary")
_SOURCE_UNIQUE_CONSTRAINT = "uq_sources_kind_owner_external_key"
# Postgres allows 65535 bind parameters per statement; bulk inserts go in chunks well below it.
_INSERT_CHUNK_ROWS = 1000


def _chunks(rows: Sequence[dict[str, object]]) -> Iterator[Sequence[dict[str, object]]]:
    for start in range(0, len(rows), _INSERT_CHUNK_ROWS):
        yield rows[start : start + _INSERT_CHUNK_ROWS]


def _visible_to(user_id: UUID) -> Any:
    """Public sources, plus the user's own. Another user's owned source is never visible."""
    return or_(Source.owner_user_id.is_(None), Source.owner_user_id == user_id)


async def get_source_by_id(session: AsyncSession, source_id: UUID) -> Source | None:
    return await session.scalar(select(Source).where(Source.id == source_id))


async def get_or_create_source(
    session: AsyncSession,
    *,
    kind: str,
    owner_user_id: UUID | None,
    external_key: str,
    label: str,
    url: str | None,
) -> Source:
    """Return the source keyed by `(kind, owner_user_id, external_key)`, creating it if needed.

    Public sources pass `owner_user_id=None`; the unique constraint treats NULLs as equal.
    """
    stmt = pg_insert(Source).values(
        kind=kind,
        owner_user_id=owner_user_id,
        external_key=external_key,
        label=label,
        url=url,
    )
    stmt = (
        stmt.on_conflict_do_update(
            constraint=_SOURCE_UNIQUE_CONSTRAINT,
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
    next_refresh_at: datetime | None = None,
) -> Source:
    values: dict[str, object] = {
        "item_count": item_count,
        "last_error": last_error,
        "updated_at": func.now(),
    }
    if last_refreshed_at is not None:
        values["last_refreshed_at"] = last_refreshed_at
    if next_refresh_at is not None:
        values["next_refresh_at"] = next_refresh_at
    stmt = (
        update(Source)
        .where(Source.id == source_id)
        .values(**values)
        .returning(Source)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def set_source_item_count(
    session: AsyncSession,
    *,
    source_id: UUID,
    item_count: int,
) -> None:
    await session.execute(
        update(Source)
        .where(Source.id == source_id)
        .values(item_count=item_count, updated_at=func.now())
    )


async def set_source_error(session: AsyncSession, *, source_id: UUID, error: str) -> None:
    await session.execute(
        update(Source).where(Source.id == source_id).values(last_error=error, updated_at=func.now())
    )


async def claim_due_rss_source(
    session: AsyncSession,
    *,
    next_refresh_at: datetime,
) -> Source | None:
    """Claim one due public RSS source that has a subscriber, and schedule its next refresh.

    `FOR UPDATE SKIP LOCKED` lets concurrent pollers each take a different source. The caller
    commits to release the row; until then no other poller can claim it.
    """
    source = await session.scalar(
        select(Source)
        .where(
            Source.kind == "lb_diary_rss",
            or_(Source.next_refresh_at.is_(None), Source.next_refresh_at <= func.now()),
            exists().where(UserSource.source_id == Source.id),
        )
        .order_by(Source.next_refresh_at.asc().nullsfirst(), Source.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True, of=Source)
    )
    if source is None:
        return None
    source.next_refresh_at = next_refresh_at
    await session.flush()
    return source


async def count_source_items(session: AsyncSession, *, source_id: UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(SourceItem).where(SourceItem.source_id == source_id)
    )
    return int(count or 0)


async def list_user_sources(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[tuple[Source, UserSource]]:
    rows = await session.execute(
        select(Source, UserSource)
        .join(UserSource, UserSource.source_id == Source.id)
        .where(UserSource.user_id == user_id, _visible_to(user_id))
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
            .where(
                UserSource.user_id == user_id,
                UserSource.source_id == source_id,
                _visible_to(user_id),
            )
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


async def list_user_self_source_ids(session: AsyncSession, *, user_id: UUID) -> list[UUID]:
    """The user's `self` sources, oldest subscription first (the order precedence uses)."""
    rows = await session.scalars(
        select(UserSource.source_id)
        .where(UserSource.user_id == user_id, UserSource.role == "self")
        .order_by(UserSource.created_at.asc(), UserSource.source_id.asc())
    )
    return list(rows)


async def delete_source(session: AsyncSession, *, source_id: UUID) -> None:
    """Delete a source. Cascades to its items, issues, subscriptions, and watch history rows."""
    await session.execute(delete(Source).where(Source.id == source_id))


async def ensure_media_item_stubs(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    """Insert unhydrated MediaItem stubs if they do not exist yet."""
    # Sorted so concurrent callers take row locks in the same order.
    ordered = sorted(
        {(r["media_type"], r["tmdb_id"]): r for r in rows}.values(),
        key=lambda r: (str(r["media_type"]), int(str(r["tmdb_id"]))),
    )
    for chunk in _chunks(ordered):
        await session.execute(
            pg_insert(MediaItem)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=["media_type", "tmdb_id"])
        )


async def upsert_source_items(
    session: AsyncSession,
    rows: list[dict[str, object]],
) -> None:
    """Insert items, or overwrite the existing item with the same key. Rows must be unique."""
    for chunk in _chunks(rows):
        stmt = pg_insert(SourceItem).values(list(chunk))
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["source_id", "media_type", "tmdb_id", "item_kind"],
                set_={
                    "rating": stmt.excluded.rating,
                    "watched_at": stmt.excluded.watched_at,
                    "entry_url": stmt.excluded.entry_url,
                    "updated_at": func.now(),
                },
            )
        )


async def replace_source_items(
    session: AsyncSession,
    *,
    source_id: UUID,
    rows: list[dict[str, object]],
) -> None:
    """Make the source's items exactly `rows`: delete the others, upsert the rest."""
    await session.execute(delete(SourceItem).where(SourceItem.source_id == source_id))
    await upsert_source_items(session, rows)


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


async def upsert_owned_watch_history(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
    rows: list[dict[str, object]],
) -> None:
    """Write `rows` as watch history owned by `source_id`.

    Inserts titles the user has no row for, and overwrites rows this source already owns. A row
    that is manual (`source_id IS NULL`) or owned by another source is left as it is.
    """
    payload = [{**r, "user_id": user_id, "source_id": source_id} for r in rows]
    for chunk in _chunks(payload):
        stmt = pg_insert(WatchHistory).values(list(chunk))
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["user_id", "media_type", "tmdb_id"],
                set_={
                    "status": stmt.excluded.status,
                    "rating": stmt.excluded.rating,
                    "watched_at": stmt.excluded.watched_at,
                    "source": stmt.excluded.source,
                    "updated_at": func.now(),
                },
                where=WatchHistory.source_id == stmt.excluded.source_id,
            )
        )


async def delete_stale_owned_watch_history(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
) -> None:
    """Delete rows `source_id` owns for the user whose title is no longer among its signals."""
    has_signal = (
        select(SourceItem.source_id)
        .where(
            SourceItem.source_id == source_id,
            SourceItem.media_type == WatchHistory.media_type,
            SourceItem.tmdb_id == WatchHistory.tmdb_id,
            SourceItem.item_kind.in_(_WATCH_SIGNAL_KINDS),
        )
        .exists()
    )
    await session.execute(
        delete(WatchHistory).where(
            WatchHistory.user_id == user_id,
            WatchHistory.source_id == source_id,
            ~has_signal,
        )
    )


async def delete_watch_history_owned_outside(
    session: AsyncSession,
    *,
    user_id: UUID,
    keep_source_ids: Sequence[UUID],
) -> None:
    """Delete the user's rows owned by any source not in `keep_source_ids`. Manual rows stay."""
    await session.execute(
        delete(WatchHistory).where(
            WatchHistory.user_id == user_id,
            WatchHistory.source_id.is_not(None),
            WatchHistory.source_id.not_in(list(keep_source_ids)),
        )
    )


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
        existing.rating = rating
        existing.watched_at = watched_at
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


async def delete_pending_import_issues_except(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
    keep_issue_ids: Sequence[UUID],
) -> None:
    """Delete the source's pending issues that a fresh import did not report again."""
    await session.execute(
        delete(ImportIssue).where(
            ImportIssue.user_id == user_id,
            ImportIssue.source_id == source_id,
            ImportIssue.status == "pending",
            ImportIssue.id.not_in(list(keep_issue_ids)),
        )
    )


async def create_upload(
    session: AsyncSession,
    *,
    user_id: UUID,
    filename: str,
    data: bytes,
) -> SourceUpload:
    upload = SourceUpload(user_id=user_id, filename=filename, data=data, size_bytes=len(data))
    session.add(upload)
    await session.flush()
    return upload


async def get_upload(
    session: AsyncSession,
    *,
    user_id: UUID,
    upload_id: UUID,
) -> SourceUpload | None:
    return await session.scalar(
        select(SourceUpload).where(SourceUpload.user_id == user_id, SourceUpload.id == upload_id)
    )


async def delete_upload(session: AsyncSession, *, user_id: UUID, upload_id: UUID) -> None:
    await session.execute(
        delete(SourceUpload).where(SourceUpload.user_id == user_id, SourceUpload.id == upload_id)
    )
