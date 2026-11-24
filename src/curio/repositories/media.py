"""Shared media cache persistence (`media_items`)."""

import logging
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.catalogs import MediaItem

MediaKey = tuple[str, int]

log = logging.getLogger(__name__)


async def get_hydrated(
    session: AsyncSession, keys: Sequence[MediaKey], *, max_age_days: int
) -> tuple[dict[MediaKey, MediaItem], set[MediaKey]]:
    """Cache lookup: rows for `keys` that have already been hydrated from TMDB.

    Returns the rows and, separately, the keys among them that are stale: hydrated more than
    `max_age_days` ago. A row exactly `max_age_days` old is still fresh. Staleness is decided by
    the database clock (`now()`), the same one that stamps `hydrated_at`.
    """
    if not keys:
        return {}, set()
    stale = MediaItem.hydrated_at < func.now() - timedelta(days=max_age_days)
    rows = await session.execute(
        select(MediaItem, stale).where(
            tuple_(MediaItem.media_type, MediaItem.tmdb_id).in_(list(keys)),
            MediaItem.hydrated_at.is_not(None),
        )
    )
    items: dict[MediaKey, MediaItem] = {}
    stale_keys: set[MediaKey] = set()
    for item, is_stale in rows:
        key = (item.media_type, item.tmdb_id)
        items[key] = item
        if is_stale:
            stale_keys.add(key)
    return items, stale_keys


async def touch_hydrated(session: AsyncSession, key: MediaKey) -> MediaItem | None:
    """Mark an already-hydrated row as freshly checked without changing its data."""
    stmt = (
        update(MediaItem)
        .where(
            MediaItem.media_type == key[0],
            MediaItem.tmdb_id == key[1],
            MediaItem.hydrated_at.is_not(None),
        )
        .values(hydrated_at=func.now(), updated_at=func.now())
        .returning(MediaItem)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one_or_none()


async def upsert_hydrated(
    session: AsyncSession,
    *,
    media_type: str,
    tmdb_id: int,
    imdb_id: str | None,
    name: str,
    year: int | None,
    description: str | None,
    rating: float | None,
    poster_path: str | None,
    poster_url: str | None,
    genres: list[str],
    keywords: list[str],
    cast_names: list[str],
    directors: list[str],
    genre_ids: list[int],
    keyword_ids: list[int],
    vote_count: int | None,
    popularity: float | None,
    original_language: str | None,
    runtime: int | None,
    release_date: date | None,
    collection_id: int | None,
    collection_name: str | None,
) -> MediaItem:
    values = {
        "imdb_id": imdb_id,
        "name": name,
        "year": year,
        "description": description,
        "rating": rating,
        "poster_path": poster_path,
        "poster_url": poster_url,
        "genres": genres,
        "keywords": keywords,
        "cast_names": cast_names,
        "directors": directors,
        "genre_ids": genre_ids,
        "keyword_ids": keyword_ids,
        "vote_count": vote_count,
        "popularity": popularity,
        "original_language": original_language,
        "runtime": runtime,
        "release_date": release_date,
        "collection_id": collection_id,
        "collection_name": collection_name,
        "hydrated_at": func.now(),
        "updated_at": func.now(),
    }

    async def run(row_values: dict) -> MediaItem:
        stmt = (
            pg_insert(MediaItem)
            .values(media_type=media_type, tmdb_id=tmdb_id, **row_values)
            .on_conflict_do_update(index_elements=["media_type", "tmdb_id"], set_=row_values)
            .returning(MediaItem)
            .execution_options(populate_existing=True)
        )
        return (await session.scalars(stmt)).one()

    try:
        # Savepoint, so a failed insert doesn't poison the caller's transaction.
        async with session.begin_nested():
            return await run(values)
    except IntegrityError:
        if imdb_id is None:
            raise
    # Another title already owns this IMDb ID (a seeded row, or a concurrent hydration of a
    # duplicate TMDB entry). Keep the metadata; leave imdb_id empty so the title isn't served.
    log.warning(
        "imdb_id collision: stored %s %s without imdb_id %s, already owned by another title",
        media_type,
        tmdb_id,
        imdb_id,
    )
    async with session.begin_nested():
        return await run({**values, "imdb_id": None})


async def list_missing_embeddings(
    session: AsyncSession, keys: Sequence[MediaKey]
) -> list[MediaItem]:
    """Hydrated rows among `keys` that have no embedding yet."""
    if not keys:
        return []
    rows = await session.scalars(
        select(MediaItem)
        .where(
            tuple_(MediaItem.media_type, MediaItem.tmdb_id).in_(list(keys)),
            MediaItem.hydrated_at.is_not(None),
            MediaItem.embedding.is_(None),
        )
        .order_by(MediaItem.media_type, MediaItem.tmdb_id)
    )
    return list(rows)


async def set_embedding(
    session: AsyncSession, *, media_type: str, tmdb_id: int, embedding: list[float]
) -> None:
    await session.execute(
        update(MediaItem)
        .where(
            MediaItem.media_type == media_type,
            MediaItem.tmdb_id == tmdb_id,
            MediaItem.embedding.is_(None),
        )
        .values(embedding=embedding, updated_at=func.now())
    )
