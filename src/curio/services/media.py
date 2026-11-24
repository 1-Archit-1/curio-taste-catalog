"""Shared media cache: hydrate TMDB results once, embed once, reuse across users.

TMDB discover, recommendation and similar-title results carry only IDs. `hydrate_titles` turns
refs into cached `media_items` rows, calling TMDB only for refs that were never hydrated or whose
row is older than `tmdb_cache_max_age_days`. If a refresh of a stale title fails, the cached row
is served instead (see `hydrate_titles` for the exact rule). Rows without an IMDb ID are stored
(so they are not refetched before they go stale, and history can still reference them; ADR 0007)
but the `*_titles` helpers drop them, since they can't be served to Stremio. The
`*_unexcluded_titles` helpers walk result pages and skip excluded titles before hydrating them.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from collections.abc import Set as AbstractSet
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.embeddings import GeminiEmbedder
from curio.clients.providers import KeyType
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import (
    MediaType,
    TmdbAuthError,
    TmdbClient,
    TmdbError,
    TmdbRef,
    TmdbTitle,
)
from curio.db.models.catalogs import MediaItem
from curio.repositories import media as media_repo
from curio.services import keys as keys_service
from curio.settings import Settings

MediaKey = tuple[str, int]
HYDRATE_CONCURRENCY = 8

log = logging.getLogger(__name__)


def build_rate_limiter(settings: Settings) -> RateLimiter:
    """One per process, shared by every TMDB client in it."""
    return RateLimiter(settings.tmdb_requests_per_second_per_process)


async def tmdb_client_for_user(
    session: AsyncSession,
    *,
    audit_session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    job_id: str | None,
    settings: Settings,
    limiter: RateLimiter,
) -> TmdbClient:
    """Build a TMDB client with the user's key, obtained only via the key-access function."""
    key = await keys_service.get_tmdb_key(
        session,
        audit_session_factory=audit_session_factory,
        user_id=user_id,
        job_id=job_id,
        reason="tmdb hydration",
        private_key=settings.key_private_key,
    )
    return TmdbClient(key, limiter)


async def embedder_for_user(
    session: AsyncSession,
    *,
    audit_session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    job_id: str | None,
    settings: Settings,
) -> GeminiEmbedder:
    """Build an embedder with the user's Gemini key, obtained only via the key-access function."""
    resolved = await keys_service.get_llm_key(
        session,
        audit_session_factory=audit_session_factory,
        user_id=user_id,
        job_id=job_id,
        reason="title embedding",
        private_key=settings.key_private_key,
        provider=KeyType.GEMINI,
    )
    return GeminiEmbedder(
        resolved.api_key, settings.embedding_model, batch_size=settings.embedding_batch_size
    )


def _dedupe(refs: Iterable[TmdbRef]) -> list[MediaKey]:
    return list(dict.fromkeys((r.media_type, r.tmdb_id) for r in refs))


class _Skipped(Exception):
    """A fetch that never started because an earlier one hit a `TmdbAuthError`."""


async def hydrate_titles(
    session: AsyncSession,
    client: TmdbClient,
    refs: Iterable[TmdbRef],
    *,
    settings: Settings,
) -> list[MediaItem]:
    """Return cached rows for `refs`, in order, hydrating (once) any that are missing.

    Refs fall into three groups: fresh (served from the cache), never hydrated (fetched), and
    stale (hydrated more than `settings.tmdb_cache_max_age_days` ago; fetched again to refresh).

    Failure rule:
    - A failed fetch of a never-hydrated title raises, as before. Titles that were already
      fetched are written to `session` first and the first error is then raised (a
      `TmdbAuthError` wins). Those writes survive only if the caller commits before re-raising;
      a caller that rolls back on exception (like `api.deps.get_session`) discards them.
    - A failed refresh of a stale title (any `TmdbError`: 401, 429, 5xx, timeout) is logged as a
      warning and the cached row is returned instead. Only one exception: when a never-hydrated
      title could not be fetched, a stale title's `TmdbAuthError` is raised too, so the key gets
      paused. A batch that is entirely stale therefore never raises a `TmdbError`.
    - A stale title that TMDB now reports as not found keeps its cached row, and its
      `hydrated_at` is bumped so it isn't refetched on every call.

    Refs that TMDB reports as not found and that were never hydrated are skipped. Fetches run
    with bounded concurrency. A `TmdbAuthError` also stops fetches that haven't started, so a
    revoked key doesn't send one failing request per title.

    Rows can include titles without an IMDb ID; callers that serve titles must filter them.
    Two workers may fetch the same new title at once. The upsert keeps the data consistent; the
    cost is one duplicate TMDB request, which we accept. Rows are written in sorted key order
    so concurrent callers can't deadlock on each other's row locks. Commit before calling
    `embed_missing` so row locks aren't held across Gemini network calls.
    """
    keys = _dedupe(refs)
    cached, stale = await media_repo.get_hydrated(
        session, keys, max_age_days=settings.tmdb_cache_max_age_days
    )
    never = [k for k in keys if k not in cached]
    to_fetch = never + [k for k in keys if k in stale]
    gate = asyncio.Semaphore(HYDRATE_CONCURRENCY)
    auth_failed = asyncio.Event()

    async def fetch(key: MediaKey) -> TmdbTitle | None:
        async with gate:
            if auth_failed.is_set():
                raise _Skipped
            try:
                return await client.hydrate(key[0], key[1])  # type: ignore[arg-type]
            except TmdbAuthError:
                auth_failed.set()
                raise

    outcomes = dict(
        zip(
            to_fetch,
            await asyncio.gather(*(fetch(k) for k in to_fetch), return_exceptions=True),
            strict=True,
        )
    )
    errors = {k: o for k, o in outcomes.items() if isinstance(o, BaseException)}
    never_failed = any(k in errors for k in never)
    fatal: list[BaseException] = []
    for key, error in errors.items():
        if isinstance(error, _Skipped):
            continue
        if key in stale and isinstance(error, TmdbError):
            log.warning(
                "TMDB refresh of %s %s failed (%s%s); serving the cached row",
                key[0],
                key[1],
                type(error).__name__,
                f", HTTP {error.status_code}" if error.status_code else "",
            )
            if never_failed and isinstance(error, TmdbAuthError):
                fatal.append(error)
        else:
            fatal.append(error)
    first_error = next((e for e in fatal if isinstance(e, TmdbAuthError)), None) or (
        fatal[0] if fatal else None
    )

    # Titles that came back (new, refreshed) or that TMDB no longer has (stale: bump only).
    writes = sorted(
        (k, o)
        for k, o in outcomes.items()
        if isinstance(o, TmdbTitle) or (o is None and k in stale)
    )
    for key, title in writes:
        if title is None:
            cached[key] = await media_repo.touch_hydrated(session, key) or cached[key]
            continue
        poster = (
            f"{settings.tmdb_poster_base_url}{title.poster_path}" if title.poster_path else None
        )
        cached[key] = await media_repo.upsert_hydrated(
            session,
            media_type=title.media_type,
            tmdb_id=title.tmdb_id,
            imdb_id=title.imdb_id,
            name=title.name,
            year=title.year,
            description=title.description,
            rating=title.rating,
            poster_path=title.poster_path,
            poster_url=poster,
            genres=title.genres,
            keywords=title.keywords,
            cast_names=title.cast,
            directors=title.directors,
            genre_ids=title.genre_ids,
            keyword_ids=title.keyword_ids,
            vote_count=title.vote_count,
            popularity=title.popularity,
            original_language=title.original_language,
            runtime=title.runtime,
            release_date=title.release_date,
            collection_id=title.collection_id,
            collection_name=title.collection_name,
        )
    if first_error is not None:
        raise first_error
    return [cached[k] for k in keys if k in cached]


async def discover_titles(
    session: AsyncSession,
    client: TmdbClient,
    media_type: MediaType,
    *,
    settings: Settings,
    page: int = 1,
    **filters: object,
) -> list[MediaItem]:
    refs = await client.discover(media_type, page=page, **filters)
    return [i for i in await hydrate_titles(session, client, refs, settings=settings) if i.imdb_id]


async def recommend_titles(
    session: AsyncSession,
    client: TmdbClient,
    media_type: MediaType,
    tmdb_id: int,
    *,
    settings: Settings,
    page: int = 1,
) -> list[MediaItem]:
    refs = await client.recommendations(media_type, tmdb_id, page=page)
    return [i for i in await hydrate_titles(session, client, refs, settings=settings) if i.imdb_id]


async def similar_titles(
    session: AsyncSession,
    client: TmdbClient,
    media_type: MediaType,
    tmdb_id: int,
    *,
    settings: Settings,
    page: int = 1,
) -> list[MediaItem]:
    refs = await client.similar(media_type, tmdb_id, page=page)
    return [i for i in await hydrate_titles(session, client, refs, settings=settings) if i.imdb_id]


async def collect_unexcluded_titles(
    session: AsyncSession,
    client: TmdbClient,
    fetch_page: Callable[[int], Awaitable[list[TmdbRef]]],
    *,
    limit: int,
    excluded_keys: AbstractSet[MediaKey] = frozenset(),
    settings: Settings,
) -> list[MediaItem]:
    """Walk paginated TMDB refs from page 1 until `limit` unexcluded servable items are collected.

    Stops early on an empty page or at `settings.tmdb_max_pages`. Excluded and duplicate refs are
    skipped before hydration so we don't spend TMDB detail calls on titles the user already saw
    or vetoed.
    """
    if limit <= 0:
        return []
    collected: list[MediaItem] = []
    seen_keys: set[MediaKey] = set()
    for page in range(1, settings.tmdb_max_pages + 1):
        refs = await fetch_page(page)
        if not refs:
            break
        candidate_refs: list[TmdbRef] = []
        for ref in refs:
            key = (ref.media_type, ref.tmdb_id)
            if key in excluded_keys or key in seen_keys:
                continue
            seen_keys.add(key)
            candidate_refs.append(ref)
        if not candidate_refs:
            continue
        hydrated = await hydrate_titles(session, client, candidate_refs, settings=settings)
        for item in hydrated:
            if item.imdb_id:
                collected.append(item)
                if len(collected) >= limit:
                    return collected
    return collected


async def discover_unexcluded_titles(
    session: AsyncSession,
    client: TmdbClient,
    media_type: MediaType,
    *,
    limit: int,
    excluded_keys: AbstractSet[MediaKey] = frozenset(),
    settings: Settings,
    **filters: object,
) -> list[MediaItem]:
    """Walk TMDB discover pages until `limit` unexcluded servable titles are collected."""
    return await collect_unexcluded_titles(
        session,
        client,
        lambda page: client.discover(media_type, page=page, **filters),
        limit=limit,
        excluded_keys=excluded_keys,
        settings=settings,
    )


async def recommend_unexcluded_titles(
    session: AsyncSession,
    client: TmdbClient,
    media_type: MediaType,
    tmdb_id: int,
    *,
    limit: int,
    excluded_keys: AbstractSet[MediaKey] = frozenset(),
    settings: Settings,
) -> list[MediaItem]:
    """Walk TMDB recommendation pages until `limit` unexcluded servable titles are collected."""
    return await collect_unexcluded_titles(
        session,
        client,
        lambda page: client.recommendations(media_type, tmdb_id, page=page),
        limit=limit,
        excluded_keys=excluded_keys,
        settings=settings,
    )


async def similar_unexcluded_titles(
    session: AsyncSession,
    client: TmdbClient,
    media_type: MediaType,
    tmdb_id: int,
    *,
    limit: int,
    excluded_keys: AbstractSet[MediaKey] = frozenset(),
    settings: Settings,
) -> list[MediaItem]:
    """Walk TMDB similar-title pages until `limit` unexcluded servable titles are collected."""
    return await collect_unexcluded_titles(
        session,
        client,
        lambda page: client.similar(media_type, tmdb_id, page=page),
        limit=limit,
        excluded_keys=excluded_keys,
        settings=settings,
    )


def embedding_text(item: MediaItem) -> str:
    """Text embedded for a title. Cast is stored for display and filtering but left out here."""
    parts = [f"{item.name} ({item.year})" if item.year else item.name]
    if item.genres:
        parts.append("Genres: " + ", ".join(item.genres))
    if item.keywords:
        parts.append("Keywords: " + ", ".join(item.keywords))
    if item.directors:
        parts.append("Director: " + ", ".join(item.directors))
    if item.description:
        parts.append(item.description)
    return "\n".join(parts)


async def embed_missing(
    session: AsyncSession, embedder: GeminiEmbedder, keys: Sequence[MediaKey]
) -> int:
    """Embed hydrated titles among `keys` that lack an embedding. Returns how many were stored.

    Vectors are written to `session` after each batch, and a failure on a later batch raises
    after earlier batches were written. Those writes survive only if the caller commits before
    re-raising; a caller that rolls back on exception (like `api.deps.get_session`) discards
    them, along with the Gemini spend they represent.
    """
    items = await media_repo.list_missing_embeddings(session, keys)
    for start in range(0, len(items), embedder.batch_size):
        batch = items[start : start + embedder.batch_size]
        vectors = await embedder.embed([embedding_text(i) for i in batch])
        for item, vector in zip(batch, vectors, strict=True):
            await media_repo.set_embedding(
                session, media_type=item.media_type, tmdb_id=item.tmdb_id, embedding=vector
            )
    return len(items)
