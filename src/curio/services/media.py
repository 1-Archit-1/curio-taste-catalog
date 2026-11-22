"""Shared media cache: hydrate TMDB results once, embed once, reuse across users.

TMDB discover and recommendation results carry only IDs. `hydrate_titles` turns refs into
cached `media_items` rows, calling TMDB only for refs not yet hydrated. Rows without an IMDb ID
are stored (so they are never refetched, and history can still reference them; ADR 0007) but
`discover_titles` and `recommend_titles` drop them, since they can't be served to Stremio.
"""
import asyncio
from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.embeddings import GeminiEmbedder
from curio.clients.providers import KeyType
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import MediaType, TmdbAuthError, TmdbClient, TmdbRef, TmdbTitle
from curio.db.models.catalogs import MediaItem
from curio.repositories import media as media_repo
from curio.services import keys as keys_service
from curio.settings import Settings

MediaKey = tuple[str, int]
HYDRATE_CONCURRENCY = 8


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


async def hydrate_titles(
    session: AsyncSession,
    client: TmdbClient,
    refs: Iterable[TmdbRef],
    *,
    settings: Settings,
) -> list[MediaItem]:
    """Return cached rows for `refs`, in order, hydrating (once) any that are missing.

    Refs that TMDB reports as not found are skipped. Fetches run with bounded concurrency. If
    one fails, titles that were already fetched are written to `session` and the first error is
    then raised. Those writes survive only if the caller commits before re-raising; a caller
    that rolls back on exception (like `api.deps.get_session`) discards them. A
    `TmdbAuthError` also stops fetches that haven't started, so a revoked key doesn't send one
    failing request per title.
    Rows can include titles without an IMDb ID; callers that serve titles must filter them.

    Two workers may fetch the same new title at once. The upsert keeps the data consistent; the
    cost is one duplicate TMDB request, which we accept. Rows are written in sorted key order
    so concurrent callers can't deadlock on each other's row locks. Commit before calling
    `embed_missing` so row locks aren't held across Gemini network calls.
    """
    keys = _dedupe(refs)
    cached = await media_repo.get_hydrated(session, keys)
    missing = [k for k in keys if k not in cached]
    gate = asyncio.Semaphore(HYDRATE_CONCURRENCY)
    auth_failed = asyncio.Event()

    async def fetch(key: MediaKey) -> TmdbTitle | None:
        async with gate:
            if auth_failed.is_set():
                return None
            try:
                return await client.hydrate(key[0], key[1])  # type: ignore[arg-type]
            except TmdbAuthError:
                auth_failed.set()
                raise

    results = await asyncio.gather(*(fetch(k) for k in missing), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    first_error = next((e for e in errors if isinstance(e, TmdbAuthError)), None) or (
        errors[0] if errors else None
    )
    fetched = [r for r in results if isinstance(r, TmdbTitle)]
    for title in sorted(fetched, key=lambda t: (t.media_type, t.tmdb_id)):
        poster = (
            f"{settings.tmdb_poster_base_url}{title.poster_path}" if title.poster_path else None
        )
        cached[(title.media_type, title.tmdb_id)] = await media_repo.upsert_hydrated(
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
