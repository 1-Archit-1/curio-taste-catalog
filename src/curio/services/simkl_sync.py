"""Simkl sync and revocation, run by workers (#6).

Only workers hold the private key (ADR 0002), so token refresh, the library pull and revocation
all run here as jobs (`simkl_sync`, `simkl_revoke`).

Sync follows Simkl's two-phase model (https://api.simkl.org/guides/sync):

* `/sync/activities` is read first and kept as the new snapshot. The first sync pulls every
  type in full (that response carries `user_rating`, so no ratings call). Later syncs compare
  the snapshot bucket by bucket and ask only for what moved, with `date_from` set to the saved
  snapshot's top-level `all`, exactly as Simkl returned it.
* A moved `rated_at` is the one case where `/sync/ratings` is called (with `date_from`, so
  removed ratings arrive as `user_rating: null`). A moved `removed_from_list` triggers an
  id-only fetch and a diff, because removals never appear in a delta.
* The snapshot is saved in the transaction that commits the items, so it can never be ahead of
  them. A failed sync keeps the old snapshot and the next one asks for the same changes again.

Items are written to `source_items`, and only `resync_watch_history` turns them into watch
history, so the provenance rules of ADR 0009 apply.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.simkl import (
    SIMKL_TYPES,
    MediaType,
    SimklAuthError,
    SimklClient,
    SimklConfigError,
    SimklEntry,
    SimklError,
    SimklItems,
    SimklType,
)
from curio.db.models.jobs import Job
from curio.db.models.simkl import SimklConnection
from curio.repositories import simkl as simkl_repo
from curio.repositories import sources as sources_repo
from curio.services import keys as keys_service
from curio.services import sources as sources_service
from curio.settings import Settings

log = logging.getLogger(__name__)

EmitStep = Callable[[str, str, dict[str, Any] | None], Awaitable[None]]

# Refresh when the access token (7 days) has a day or less left.
# https://api.simkl.org/api-reference/oauth2-tokens
REFRESH_WITHIN = timedelta(hours=24)
UNEXPECTED_ERROR = "Unexpected error while syncing Simkl. See worker logs."

# `/sync/all-items/{type}` takes `shows`; `/sync/activities` names the same bucket `tv_shows`.
_ACTIVITY_BUCKET: dict[SimklType, str] = {"shows": "tv_shows", "movies": "movies", "anime": "anime"}
_LIST_BUCKETS = ("watching", "plantowatch", "hold", "completed", "dropped")
# Simkl's `completed` is `watched` here. `plantowatch` and `hold` aren't watch history.
_KIND_BY_STATUS = {"completed": "watched", "watching": "watching", "dropped": "dropped"}
# Only anime can put two Simkl entries on one TMDB title (a show split into seasons or parts).
# The one that says most about the user's current state wins.
_KIND_RANK = {"watching": 0, "watched": 1, "dropped": 2}

Title = tuple[MediaType, int]


@dataclass
class SyncPlan:
    """What a sync has to fetch, decided from the saved and the new activities."""

    full: bool = False
    date_from: str | None = None
    list_types: list[SimklType] = field(default_factory=list)
    rating_types: list[SimklType] = field(default_factory=list)
    check_removals: bool = False

    @property
    def is_noop(self) -> bool:
        return not (self.full or self.list_types or self.rating_types or self.check_removals)


def _bucket(activities: object, name: str) -> dict[str, Any]:
    value = activities.get(name) if isinstance(activities, dict) else None
    return value if isinstance(value, dict) else {}


def plan_sync(saved: dict[str, Any] | None, new: dict[str, Any]) -> SyncPlan:
    """Compare the new activities with the saved snapshot.

    No snapshot, or one taken before the account had any activity, means a full pull. An
    unchanged top-level `all` means nothing moved anywhere. Otherwise each type's buckets are
    compared one by one, and `date_from` is the saved snapshot's `all`.
    """
    if saved is None:
        return SyncPlan(full=True)
    saved_all, new_all = saved.get("all"), new.get("all")
    if new_all == saved_all:
        return SyncPlan()
    if not isinstance(saved_all, str):
        return SyncPlan(full=True)
    plan = SyncPlan(date_from=saved_all)
    for simkl_type in SIMKL_TYPES:
        now = _bucket(new, _ACTIVITY_BUCKET[simkl_type])
        before = _bucket(saved, _ACTIVITY_BUCKET[simkl_type])
        if any(now.get(name) != before.get(name) for name in _LIST_BUCKETS):
            plan.list_types.append(simkl_type)
        if now.get("rated_at") != before.get("rated_at"):
            plan.rating_types.append(simkl_type)
        if now.get("removed_from_list") != before.get("removed_from_list"):
            plan.check_removals = True
    return plan


@dataclass
class SyncResult:
    synced: int = 0
    removed: int = 0
    skipped_without_tmdb: int = 0
    unchanged: bool = False


def reduce_entries(
    entries: Iterable[SimklEntry],
) -> tuple[dict[Title, dict[str, Any] | None], list[dict[str, object]]]:
    """Reduce Simkl entries to one item (or none) per title, and the media stubs they need.

    `completed` becomes `watched`; `watching` and `dropped` keep their names. A title that only
    has `plantowatch` or `hold` entries maps to `None`: it isn't history, so any stored item
    for it has to go. The rating is Simkl's `user_rating` as it is, 1-10 (ADR 0009).
    """
    grouped: dict[Title, list[SimklEntry]] = {}
    for entry in entries:
        grouped.setdefault((entry.media_type, entry.tmdb_id), []).append(entry)
    items: dict[Title, dict[str, Any] | None] = {}
    stubs: list[dict[str, object]] = []
    for title, group in sorted(grouped.items()):
        counted = [e for e in group if e.status in _KIND_BY_STATUS]
        if not counted:
            items[title] = None
            continue
        best = min(counted, key=lambda e: _KIND_RANK[_KIND_BY_STATUS[e.status]])
        rating = best.rating
        if rating is None:
            rating = next((e.rating for e in counted if e.rating is not None), None)
        items[title] = {
            "media_type": title[0],
            "tmdb_id": title[1],
            "item_kind": _KIND_BY_STATUS[best.status],
            "rating": rating,
            "watched_at": best.last_watched_at,
            "entry_url": best.url,
        }
        stubs.append(
            {"media_type": title[0], "tmdb_id": title[1], "name": best.title, "year": best.year}
        )
    return items, stubs


# --- Tokens -----------------------------------------------------------------------------------


async def _access_token(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: SimklClient,
    job: Job,
    force_refresh: bool,
) -> tuple[str, bool]:
    """The user's access token, refreshed first if it is about to expire (or `force_refresh`).

    Returns `(token, refreshed)`. The connection row is locked while this runs: a grant has one
    live access token, and a refresh kills the previous one, so concurrent refreshes would keep
    invalidating each other. After a refresh the new token is committed at once, before anything
    else can fail, because the old one no longer works.
    """
    connection = await simkl_repo.get_connection(session, user_id=job.user_id, for_update=True)
    if connection is None:
        raise SimklConfigError("Simkl is not connected.")
    access, refresh = await keys_service.decrypt_simkl_secrets(
        session_factory,
        user_id=job.user_id,
        job_id=str(job.id),
        reason="simkl sync",
        private_key=settings.key_private_key,
        ciphertexts=[connection.access_token_ciphertext, connection.refresh_token_ciphertext],
    )
    expires_soon = connection.access_expires_at - datetime.now(UTC) <= REFRESH_WITHIN
    if not (force_refresh or expires_soon):
        await session.commit()  # release the lock
        return access, False
    tokens = await client.refresh(refresh)
    await simkl_repo.update_connection_tokens(
        session,
        user_id=job.user_id,
        access_token_ciphertext=keys_service.seal_in_worker(
            settings.key_private_key, tokens.access_token
        ),
        refresh_token_ciphertext=keys_service.seal_in_worker(
            settings.key_private_key, tokens.refresh_token
        ),
        access_expires_at=datetime.now(UTC) + timedelta(seconds=tokens.expires_in),
        scope=tokens.scope,
    )
    await session.commit()
    return tokens.access_token, True


# --- Sync -------------------------------------------------------------------------------------


async def _fetch(
    client: SimklClient, access_token: str, plan: SyncPlan
) -> tuple[SimklItems, set[Title] | None]:
    """Run the plan's requests, one at a time (Simkl asks for sequential calls)."""
    entries: list[SimklEntry] = []
    skipped = 0

    def take(items: SimklItems) -> None:
        nonlocal skipped
        entries.extend(items.entries)
        skipped += items.skipped_without_tmdb

    if plan.full:
        for simkl_type in SIMKL_TYPES:
            take(await client.get_all_items(access_token, simkl_type))
    else:
        if len(plan.list_types) > 1:
            take(await client.get_all_items(access_token, None, date_from=plan.date_from))
        elif plan.list_types:
            take(
                await client.get_all_items(
                    access_token, plan.list_types[0], date_from=plan.date_from
                )
            )
        for simkl_type in plan.rating_types:
            if plan.date_from is None:
                # Without `date_from` the ratings call returns the whole library.
                raise SimklError("Refusing to fetch every Simkl rating without a date_from.")
            take(await client.get_ratings_delta(access_token, simkl_type, date_from=plan.date_from))
    present: set[Title] | None = None
    if plan.check_removals:
        # All three types, so a title is only "gone" when no list holds it.
        present = set()
        for simkl_type in SIMKL_TYPES:
            present |= (await client.get_id_list(access_token, simkl_type)).titles
    return SimklItems(entries=entries, skipped_without_tmdb=skipped), present


async def _apply(
    session: AsyncSession,
    *,
    source_id: UUID,
    plan: SyncPlan,
    fetched: SimklItems,
    present: set[Title] | None,
) -> SyncResult:
    items, stubs = reduce_entries(fetched.entries)
    result = SyncResult(
        synced=sum(1 for item in items.values() if item is not None),
        skipped_without_tmdb=fetched.skipped_without_tmdb,
    )
    await sources_repo.ensure_media_item_stubs(session, stubs)
    rows = [{"source_id": source_id, **item} for item in items.values() if item is not None]
    if plan.full:
        await sources_repo.replace_source_items(session, source_id=source_id, rows=rows)
    else:
        # A title's entry replaces everything stored for it: moving from `watching` to
        # `completed` changes the item kind, which is part of the key.
        await sources_repo.delete_source_titles(session, source_id=source_id, titles=list(items))
        await sources_repo.upsert_source_items(session, rows)
    if present is not None:
        stored = await sources_repo.list_source_titles(session, source_id=source_id)
        gone = sorted(stored - present)
        await sources_repo.delete_source_titles(session, source_id=source_id, titles=gone)
        result.removed = len(gone)
    return result


def _summary(result: SyncResult) -> str:
    if result.unchanged:
        return "Nothing changed on Simkl."
    parts = [f"{result.synced} titles synced"]
    if result.removed:
        parts.append(f"{result.removed} removed")
    if result.skipped_without_tmdb:
        parts.append(f"{result.skipped_without_tmdb} skipped (no TMDB id)")
    return "Simkl: " + ", ".join(parts) + "."


async def _sync_once(
    session: AsyncSession,
    client: SimklClient,
    access_token: str,
    *,
    user_id: UUID,
    source_id: UUID,
    emit_step: EmitStep,
) -> SyncResult:
    connection = await simkl_repo.get_connection(session, user_id=user_id)
    if connection is None:
        raise SimklConfigError("Simkl is not connected.")
    saved = connection.activities_snapshot
    # Read before pulling, never after: a change made during the pull then still counts as newer
    # than this snapshot and arrives in the next delta.
    activities = await client.get_activities(access_token)
    plan = plan_sync(saved, activities)
    await emit_step("plan", "Checking Simkl for changes", None)  # commits; nothing is written yet
    if plan.is_noop:
        result = SyncResult(unchanged=True)
    else:
        fetched, present = await _fetch(client, access_token, plan)
        result = await _apply(
            session, source_id=source_id, plan=plan, fetched=fetched, present=present
        )
    await sources_repo.update_source_refresh_state(
        session,
        source_id=source_id,
        item_count=await sources_repo.count_source_items(session, source_id=source_id),
        last_refreshed_at=datetime.now(UTC),
        last_error=None,
    )
    await sources_service.resync_watch_history(session, user_id=user_id)
    await simkl_repo.record_sync_success(session, user_id=user_id, snapshot=activities)
    await session.commit()
    await emit_step(
        "sync",
        _summary(result),
        {
            "synced": result.synced,
            "removed": result.removed,
            "skipped_without_tmdb": result.skipped_without_tmdb,
            "unchanged": result.unchanged,
        },
    )
    return result


async def record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    source_id: UUID,
    exc: BaseException,
) -> None:
    """Store the error on the connection and the source, on a session of its own, so the
    rollback of the failed sync doesn't discard it.
    """
    message = str(exc) if isinstance(exc, SimklError) else UNEXPECTED_ERROR
    async with session_factory() as session:
        await simkl_repo.record_sync_failure(
            session,
            user_id=user_id,
            error=message,
            needs_reconnect=isinstance(exc, SimklAuthError),
        )
        await sources_repo.set_source_error(session, source_id=source_id, error=message)
        await session.commit()


async def run_sync_job(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    job: Job,
    emit_step: EmitStep,
) -> None:
    """Handler for `simkl_sync` jobs."""
    client_id = settings.simkl_client_id
    secret = settings.simkl_client_secret
    if not (client_id and secret):
        raise SimklConfigError("Simkl isn't set up on this server.")
    connection: SimklConnection | None = await simkl_repo.get_connection(
        session, user_id=job.user_id
    )
    if connection is None:
        raise SimklConfigError("Simkl is not connected.")
    source_id = connection.source_id
    if connection.needs_reconnect:
        raise SimklAuthError("Simkl needs to be reconnected before it can sync.")
    await session.commit()  # end the read transaction before any network call
    async with SimklClient(client_id, secret.get_secret_value()) as client:
        try:
            await emit_step("start", "Connecting to Simkl", None)
            refreshed = False
            for attempt in (1, 2):
                token, refreshed_now = await _access_token(
                    session,
                    session_factory=session_factory,
                    settings=settings,
                    client=client,
                    job=job,
                    force_refresh=attempt == 2,
                )
                refreshed = refreshed or refreshed_now
                try:
                    await _sync_once(
                        session,
                        client,
                        token,
                        user_id=job.user_id,
                        source_id=source_id,
                        emit_step=emit_step,
                    )
                    return
                except SimklAuthError:
                    # An expired token the clock didn't predict: refresh once and try again.
                    # A rejection right after a refresh means the grant itself is gone.
                    if refreshed:
                        raise
                    await session.rollback()
        except Exception as exc:
            await session.rollback()
            await record_failure(session_factory, user_id=job.user_id, source_id=source_id, exc=exc)
            raise


# --- Revocation -------------------------------------------------------------------------------


def revocation_id_of(job: Job) -> UUID | None:
    try:
        return UUID(str((job.payload or {})["revocation_id"]))
    except (KeyError, ValueError):
        return None


async def run_revoke_job(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    job: Job,
    emit_step: EmitStep,
) -> None:
    """Handler for `simkl_revoke` jobs: revoke a sealed grant at Simkl, then delete the row.

    Simkl answers 200 whether or not anything was revoked, so a clean return is all the
    confirmation there is. A row that is already gone means an earlier attempt finished.
    """
    revocation_id = revocation_id_of(job)
    if revocation_id is None:
        raise SimklConfigError("The revoke job has no revocation.")
    revocation = await simkl_repo.get_revocation(
        session, user_id=job.user_id, revocation_id=revocation_id
    )
    if revocation is None:
        return
    client_id = settings.simkl_client_id
    secret = settings.simkl_client_secret
    if not (client_id and secret):
        raise SimklConfigError("Simkl isn't set up on this server.")
    (refresh_token,) = await keys_service.decrypt_simkl_secrets(
        session_factory,
        user_id=job.user_id,
        job_id=str(job.id),
        reason="simkl revoke",
        private_key=settings.key_private_key,
        ciphertexts=[revocation.refresh_token_ciphertext],
    )
    await session.commit()
    async with SimklClient(client_id, secret.get_secret_value()) as client:
        await client.revoke(refresh_token)
    await simkl_repo.delete_revocation(session, user_id=job.user_id, revocation_id=revocation_id)
    await emit_step("revoke", "Simkl access revoked.", None)


async def discard_revocation(
    session_factory: async_sessionmaker[AsyncSession], *, job: Job
) -> None:
    """Delete a revocation's sealed token on its own session, after the last failed attempt.

    A grant that can't be revoked expires by itself (a refresh token lasts 180 days). Keeping
    its ciphertext for ever would be worse.
    """
    revocation_id = revocation_id_of(job)
    if revocation_id is None:
        return
    async with session_factory() as session:
        await simkl_repo.delete_revocation(
            session, user_id=job.user_id, revocation_id=revocation_id
        )
        await session.commit()
