"""Starter catalog generation pipeline using TMDB recommendations."""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import MediaType, TmdbClient
from curio.db.models.catalogs import MediaItem
from curio.db.models.jobs import Job, Run
from curio.repositories import catalogs as catalogs_repo
from curio.services import keys as keys_service
from curio.services import media as media_service
from curio.settings import Settings

STARTER_SEED_LIMIT = 5
STARTER_CANDIDATE_LIMIT = 50


async def run_starter_pipeline(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    rate_limiter: RateLimiter,
    job: Job,
    run: Run,
    emit_step: Callable[[str, str, dict[str, Any] | None], Awaitable[None]],
) -> None:
    """Generate initial preset candidates from watch-history seeds via TMDB recommendations."""
    del run  # Part of the pipeline runner interface; progress is emitted via `emit_step`.
    if job.preset_id is None:
        raise ValueError("Starter pipeline job requires a preset_id")

    preset = await catalogs_repo.get_preset(
        session,
        user_id=job.user_id,
        preset_id=job.preset_id,
    )
    if preset is None:
        raise ValueError(f"Preset {job.preset_id} not found for user {job.user_id}")

    media_type = cast(MediaType, preset.media_type)

    await emit_step("seeds", "Loading top-rated titles from watch history", None)
    seeds = await catalogs_repo.list_top_rated_watched_titles(
        session,
        user_id=job.user_id,
        media_type=media_type,
        limit=STARTER_SEED_LIMIT,
    )
    if not seeds:
        await emit_step(
            "seeds",
            "No watched titles found in history; 0 candidates generated",
            {"count": 0},
        )
        return

    tmdb_key = await keys_service.get_tmdb_key(
        session,
        audit_session_factory=session_factory,
        user_id=job.user_id,
        job_id=str(job.id),
        reason="starter pipeline",
        private_key=settings.key_private_key,
    )

    pool: dict[tuple[str, int], MediaItem] = {}
    async with TmdbClient(tmdb_key, rate_limiter) as client:
        await emit_step(
            "recommendations",
            f"Fetching TMDB recommendations from {len(seeds)} seed titles",
            {"seed_count": len(seeds)},
        )
        for seed_id in seeds:
            items = await media_service.recommend_titles(
                session,
                client,
                media_type,
                seed_id,
                settings=settings,
                page=1,
            )
            for item in items:
                pool.setdefault((item.media_type, item.tmdb_id), item)

    # Persist hydrated media_items before filtering/saving so TMDB work isn't lost on later errors.
    await session.commit()

    await emit_step(
        "filter_sort",
        f"Filtering and ranking {len(pool)} hydrated candidates",
        {"pool_size": len(pool)},
    )
    excluded_rows = await catalogs_repo.list_excluded_titles(session, user_id=job.user_id)
    excluded_keys = {(row.media_type, row.tmdb_id) for row in excluded_rows}

    filtered = [
        item
        for item in pool.values()
        if (item.media_type, item.tmdb_id) not in excluded_keys
        and (
            preset.min_rating is None
            or (item.rating is not None and item.rating >= preset.min_rating)
        )
    ]
    # Order by TMDB rating DESC (nulls last), then vote_count DESC, then tmdb_id ASC for stability.
    filtered.sort(
        key=lambda item: (
            item.rating is None,
            -(item.rating or 0.0),
            -(item.vote_count or 0),
            item.tmdb_id,
        )
    )
    top_candidates = filtered[:STARTER_CANDIDATE_LIMIT]

    await catalogs_repo.replace_preset_candidates(
        session,
        user_id=job.user_id,
        preset_id=preset.id,
        items=[(item.media_type, item.tmdb_id) for item in top_candidates],
    )
    await session.commit()

    await emit_step(
        "saved",
        f"Saved {len(top_candidates)} candidates",
        {"saved_count": len(top_candidates)},
    )
