"""Typed taste sources: ownership, watch history sync, Letterboxd RSS polling, and export import.

Public sources (diary RSS) are shared and fetched once however many users follow them. Export
sources belong to the user who uploaded them (ADR 0009). A user's `self` sources write their
watch history: a source writes only rows it owns or that don't exist yet, and
`resync_watch_history` is the one place that reconciles a user's history with their sources.
Everything here that needs TMDB (matching export rows, hydrating titles) runs in the worker; the
API only writes unhydrated `media_items` stubs, which carry names and years from the data at hand.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.letterboxd import (
    LetterboxdClient,
    LetterboxdError,
    LetterboxdExportArchive,
    LetterboxdExportRow,
    LetterboxdRssFeed,
    normalize_letterboxd_username,
    parse_export_zip,
    rss_url_for_username,
)
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import MediaType, TmdbAuthError, TmdbClient, TmdbRef
from curio.db.models.jobs import Job
from curio.db.models.sources import ImportIssue, Source, SourceItem, UserSource
from curio.repositories import sources as sources_repo
from curio.services import keys as keys_service
from curio.services import media as media_service
from curio.settings import Settings

log = logging.getLogger(__name__)

HIGH_CONFIDENCE_THRESHOLD = 0.85
MAX_CANDIDATE_MATCHES = 5
MATCH_CONCURRENCY = 8
IMPORT_ROLES = ("self", "taste", "candidates")
# What `watch_history.source` says about a row, by the kind of source that wrote it.
HISTORY_SOURCE_LABELS = {"simkl": "simkl"}
HISTORY_SOURCE_LABEL_DEFAULT = "letterboxd"
# Letterboxd's signals. They are also the item kinds an import issue can carry.
_WATCH_SIGNAL_KINDS = frozenset({"watched", "rating", "diary"})
# Simkl adds a status per title; the item kind of the same name becomes the history status.
_HISTORY_SIGNAL_KINDS = _WATCH_SIGNAL_KINDS | {"watching", "dropped"}
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_EARLIEST = datetime.min.replace(tzinfo=UTC)

EmitStep = Callable[[str, str, dict[str, Any] | None], Awaitable[None]]


class SourceNotFoundError(LookupError):
    """Raised when a source or user-source subscription does not exist for the user."""


class ImportIssueNotFoundError(LookupError):
    """Raised when an import issue does not exist for the user."""


class InvalidSourceOperationError(ValueError):
    """Raised when an operation is invalid for the source kind or target."""


class ImportJobInputError(ValueError):
    """Raised when an `lb_import` job has no usable upload. Retrying cannot fix it."""


@dataclass(frozen=True)
class SourceSummary:
    source: Source
    user_source: UserSource


@dataclass(frozen=True)
class ImportResult:
    sources: list[SourceSummary]
    matched_count: int
    issue_count: int
    skipped_count: int


# --- Matching -----------------------------------------------------------------------------------


def _normalize_title(value: str) -> str:
    return _NON_ALNUM_RE.sub(" ", value.lower()).strip()


def _score_candidate(query_title: str, query_year: int | None, ref: TmdbRef) -> float:
    norm_q = _normalize_title(query_title)
    norm_c = _normalize_title(ref.name or "")
    if not norm_q or not norm_c:
        return 0.0
    title_score = 1.0 if norm_q == norm_c else difflib.SequenceMatcher(None, norm_q, norm_c).ratio()
    if query_year is not None and ref.year is not None:
        diff = abs(query_year - ref.year)
        if diff == 0:
            score = 0.75 * title_score + 0.25
        elif diff == 1:
            score = 0.75 * title_score + 0.15
        else:
            score = 0.75 * title_score
    elif norm_q == norm_c:
        score = 0.85
    else:
        score = 0.70 * title_score
    return round(min(1.0, max(0.0, score)), 4)


@dataclass(frozen=True)
class TitleMatch:
    """The outcome of matching one export title: a confident ref (or `None`) and candidates."""

    ref: TmdbRef | None
    confidence: float
    candidates: list[dict[str, Any]]


MatchKey = tuple[str, int | None]


def _match_key(row: LetterboxdExportRow) -> MatchKey:
    return (_normalize_title(row.title), row.year)


def _score_refs(title: str, year: int | None, refs: list[TmdbRef]) -> list[tuple[TmdbRef, float]]:
    return [(ref, _score_candidate(title, year, ref)) for ref in refs]


async def _search(
    client: TmdbClient, media_type: MediaType, title: str, year: int | None
) -> list[TmdbRef]:
    refs = await client.search(media_type, title, year)
    if not refs and year is not None:
        # Retry without the year so near-year releases still show up as candidates.
        refs = await client.search(media_type, title)
    return refs


def _decide(scored: list[tuple[TmdbRef, float]]) -> TitleMatch:
    scored = sorted(scored, key=lambda pair: pair[1], reverse=True)
    candidates: list[dict[str, Any]] = [
        {
            "media_type": ref.media_type,
            "tmdb_id": ref.tmdb_id,
            "name": ref.name or "",
            "year": ref.year,
            "confidence": score,
        }
        for ref, score in scored[:MAX_CANDIDATE_MATCHES]
    ]
    if not scored:
        return TitleMatch(None, 0.0, candidates)
    best_ref, best_score = scored[0]
    if best_score < HIGH_CONFIDENCE_THRESHOLD:
        return TitleMatch(None, best_score, candidates)
    # An ambiguous tie at the top goes to manual review rather than guessing.
    if len(scored) > 1 and scored[1][1] == best_score:
        return TitleMatch(None, best_score, candidates)
    return TitleMatch(best_ref, best_score, candidates)


async def _match_title(client: TmdbClient, title: str, year: int | None) -> TitleMatch:
    """Match against movies first. Only without a confident movie match are TV shows scored too."""
    scored = _score_refs(title, year, await _search(client, "movie", title, year))
    match = _decide(scored)
    if match.ref is not None:
        return match
    scored += _score_refs(title, year, await _search(client, "series", title, year))
    return _decide(scored)


async def _match_rows(
    client: TmdbClient, rows: Iterable[LetterboxdExportRow]
) -> dict[MatchKey, TitleMatch]:
    """Match each distinct (title, year) once, with bounded concurrency."""
    unique: dict[MatchKey, LetterboxdExportRow] = {}
    for row in rows:
        unique.setdefault(_match_key(row), row)
    gate = asyncio.Semaphore(MATCH_CONCURRENCY)
    auth_failed = asyncio.Event()

    async def match(row: LetterboxdExportRow) -> TitleMatch | None:
        async with gate:
            if auth_failed.is_set():
                return None  # a rejected key fails every search; stop sending them
            try:
                return await _match_title(client, row.title, row.year)
            except TmdbAuthError:
                auth_failed.set()
                raise

    keys = list(unique)
    results = await asyncio.gather(*(match(unique[k]) for k in keys), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise next((e for e in errors if isinstance(e, TmdbAuthError)), errors[0])
    return {k: r for k, r in zip(keys, results, strict=True) if isinstance(r, TitleMatch)}


# --- Source items and watch history -------------------------------------------------------------


def _merge_item_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Collapse rows for the same (source, title, kind), the latest `watched_at` winning.

    Rows are folded oldest first and a later non-null field replaces an earlier one, so a rewatch
    without a rating keeps the previous rating but a newer rating replaces it. The result is
    sorted by title so concurrent writers lock rows in the same order.
    """

    def when(row: dict[str, object]) -> datetime:
        watched_at = row.get("watched_at")
        return watched_at if isinstance(watched_at, datetime) else _EARLIEST

    merged: dict[tuple[object, object, object, object], dict[str, object]] = {}
    for row in sorted(rows, key=when):  # stable: ties keep file order
        key = (row["source_id"], row["media_type"], row["tmdb_id"], row["item_kind"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
            continue
        for column in ("rating", "watched_at", "entry_url"):
            if row.get(column) is not None:
                existing[column] = row[column]
    return sorted(
        merged.values(),
        key=lambda r: (str(r["media_type"]), int(str(r["tmdb_id"])), str(r["item_kind"])),
    )


def _history_status(kinds: set[str]) -> str:
    """A title's watch history status from the item kinds a source holds for it."""
    if "watching" in kinds:
        return "watching"
    if kinds & _WATCH_SIGNAL_KINDS:
        return "watched"
    return "dropped"


def _history_rows(items: Sequence[SourceItem], *, source_label: str) -> list[dict[str, object]]:
    """One watch history row per title from a source's watch signals.

    The rating comes from `rating` items (a Letterboxd account's current rating), then from
    diary entries, then from the title's status item (`watched`, `watching`, `dropped`).
    `watched_at` is the latest date any signal has. The status is `watching` or `dropped` only
    for a source that says so (Simkl); every Letterboxd signal means `watched`.
    """
    by_title: dict[tuple[str, int], dict[str, Any]] = {}
    precedence = {"rating": 0, "diary": 1, "watched": 2, "watching": 2, "dropped": 2}
    for item in items:
        if item.item_kind not in _HISTORY_SIGNAL_KINDS:
            continue
        entry = by_title.setdefault(
            (item.media_type, item.tmdb_id),
            {"rating": None, "rank": len(precedence), "watched_at": None, "kinds": set()},
        )
        entry["kinds"].add(item.item_kind)
        rank = precedence[item.item_kind]
        if item.rating is not None and rank < entry["rank"]:
            entry["rating"], entry["rank"] = item.rating, rank
        if item.watched_at is not None and (
            entry["watched_at"] is None or item.watched_at > entry["watched_at"]
        ):
            entry["watched_at"] = item.watched_at
    return [
        {
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "status": _history_status(entry["kinds"]),
            "rating": entry["rating"],
            "watched_at": entry["watched_at"],
            "source": source_label,
        }
        for (media_type, tmdb_id), entry in sorted(by_title.items())
    ]


async def resync_watch_history(session: AsyncSession, *, user_id: UUID) -> None:
    """Make the user's source-written watch history match their current `self` sources.

    Reads only stored `source_items`, so it needs no network. Call it whenever a source's items
    or a user's role for a source change. It runs in three steps:

    1. Delete rows owned by a source the user no longer has as `self`. That also lets another
       source take over those titles in step 3.
    2. Delete rows a `self` source owns whose title it no longer has (removals, un-rates).
    3. Write each `self` source's titles, oldest subscription first. A source inserts titles with
       no row yet and overwrites rows it owns; manual rows and rows owned by another source are
       never touched.
    """
    sources = await sources_repo.list_user_self_sources(session, user_id=user_id)
    source_ids = [source.id for source in sources]
    await sources_repo.delete_watch_history_owned_outside(
        session, user_id=user_id, keep_source_ids=source_ids
    )
    for source_id in source_ids:
        await sources_repo.delete_stale_owned_watch_history(
            session, user_id=user_id, source_id=source_id
        )
    for source in sources:
        items = await sources_repo.list_source_watch_signals(session, source_id=source.id)
        label = HISTORY_SOURCE_LABELS.get(source.kind, HISTORY_SOURCE_LABEL_DEFAULT)
        await sources_repo.upsert_owned_watch_history(
            session,
            user_id=user_id,
            source_id=source.id,
            rows=_history_rows(items, source_label=label),
        )


async def _resync_users(session: AsyncSession, user_ids: Iterable[UUID]) -> None:
    for user_id in sorted(set(user_ids), key=str):
        await resync_watch_history(session, user_id=user_id)


# --- Subscriptions ------------------------------------------------------------------------------


async def subscribe_user_to_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    source: Source,
    role: str,
) -> UserSource:
    """Link the user to the source with `role`. Only an owned source's owner may subscribe."""
    if source.owner_user_id is not None and source.owner_user_id != user_id:
        raise SourceNotFoundError("Source not found.")
    return await sources_repo.upsert_user_source(
        session, user_id=user_id, source_id=source.id, role=role
    )


async def list_user_sources(session: AsyncSession, *, user_id: UUID) -> list[SourceSummary]:
    rows = await sources_repo.list_user_sources(session, user_id=user_id)
    return [SourceSummary(source=s, user_source=us) for s, us in rows]


async def update_source_role(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
    role: str,
) -> SourceSummary:
    pair = await sources_repo.get_user_source(session, user_id=user_id, source_id=source_id)
    if pair is None:
        raise SourceNotFoundError("Source not found.")
    source, _ = pair
    if source.kind == "simkl":
        raise InvalidSourceOperationError("A Simkl connection is always a 'self' source.")
    updated = await sources_repo.update_user_source_role(
        session, user_id=user_id, source_id=source_id, role=role
    )
    if updated is None:
        raise SourceNotFoundError("Source not found.")
    # Becoming `self` writes history; leaving `self` removes what this source wrote.
    await resync_watch_history(session, user_id=user_id)
    return SourceSummary(source=source, user_source=updated)


async def remove_user_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
) -> None:
    """Unsubscribe. An owned source is private to its owner, so removing it deletes it."""
    pair = await sources_repo.get_user_source(session, user_id=user_id, source_id=source_id)
    if pair is None:
        raise SourceNotFoundError("Source not found.")
    source, _ = pair
    if source.kind == "simkl":
        # Deleting the source would drop the sealed tokens before Simkl is told to revoke them.
        raise InvalidSourceOperationError("Disconnect Simkl instead of removing its source.")
    if source.owner_user_id == user_id:
        await sources_repo.delete_source(session, source_id=source_id)
    else:
        await sources_repo.delete_user_source(session, user_id=user_id, source_id=source_id)
    await resync_watch_history(session, user_id=user_id)


# --- Letterboxd diary RSS (public, shared, no TMDB key needed) -----------------------------------


async def preview_letterboxd_source(
    input_value: str,
    *,
    lb_client: LetterboxdClient | None = None,
) -> LetterboxdRssFeed:
    """Validate a Letterboxd username or URL and fetch its public RSS feed for preview."""
    username = normalize_letterboxd_username(input_value)
    client = lb_client or LetterboxdClient()
    try:
        return await client.fetch_rss(username)
    finally:
        if lb_client is None:
            await client.aclose()


def _next_refresh_at(settings: Settings) -> datetime:
    return datetime.now(UTC) + timedelta(hours=settings.rss_refresh_interval_hours)


async def ingest_rss_feed(
    session: AsyncSession,
    *,
    source: Source,
    feed: LetterboxdRssFeed,
    settings: Settings,
    extra_user_ids: Iterable[UUID] = (),
) -> Source:
    """Store a feed's entries and sync the watch history of every `self` subscriber.

    Entries have TMDB IDs, so no matching or TMDB call is needed. Titles get unhydrated stubs;
    the normal pipelines hydrate them later. The feed holds only recent entries, so items that
    are not in it are kept. `extra_user_ids` are resynced too (a user whose role just changed).
    """
    stubs: list[dict[str, object]] = []
    items: list[dict[str, object]] = []
    for entry in feed.entries:
        if entry.tmdb_id is None:
            continue
        stubs.append(
            {
                "media_type": entry.media_type,
                "tmdb_id": entry.tmdb_id,
                "name": entry.title,
                "year": entry.year,
            }
        )
        items.append(
            {
                "source_id": source.id,
                "media_type": entry.media_type,
                "tmdb_id": entry.tmdb_id,
                "item_kind": "diary",
                "rating": entry.rating,
                "watched_at": entry.watched_at,
                "entry_url": entry.entry_url,
            }
        )
    await sources_repo.ensure_media_item_stubs(session, stubs)
    await sources_repo.upsert_source_items(session, _merge_item_rows(items))
    updated = await sources_repo.update_source_refresh_state(
        session,
        source_id=source.id,
        item_count=await sources_repo.count_source_items(session, source_id=source.id),
        last_refreshed_at=datetime.now(UTC),
        last_error=None,
        next_refresh_at=_next_refresh_at(settings),
    )
    self_users = await sources_repo.list_self_user_ids_for_source(session, source_id=source.id)
    await _resync_users(session, [*self_users, *extra_user_ids])
    return updated


async def add_rss_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    input_value: str,
    role: str,
    settings: Settings,
    lb_client: LetterboxdClient | None = None,
) -> SourceSummary:
    """Subscribe `user_id` to a shared Letterboxd diary RSS source and ingest its entries."""
    username = normalize_letterboxd_username(input_value)
    feed = await preview_letterboxd_source(username, lb_client=lb_client)
    source = await sources_repo.get_or_create_source(
        session,
        kind="lb_diary_rss",
        owner_user_id=None,
        external_key=username,
        label=f"Letterboxd RSS (@{username})",
        url=rss_url_for_username(username),
    )
    user_source = await subscribe_user_to_source(session, user_id=user_id, source=source, role=role)
    updated = await ingest_rss_feed(
        session, source=source, feed=feed, settings=settings, extra_user_ids=[user_id]
    )
    return SourceSummary(source=updated, user_source=user_source)


async def record_source_error(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_id: UUID,
    error: str,
) -> None:
    """Store `last_error` on its own session, so a rollback of the caller's work keeps it."""
    async with session_factory() as session:
        await sources_repo.set_source_error(session, source_id=source_id, error=error)
        await session.commit()


async def refresh_rss_source(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    source_id: UUID,
    user_id: UUID,
    settings: Settings,
    lb_client: LetterboxdClient | None = None,
) -> SourceSummary:
    """Refresh a public `lb_diary_rss` source now and sync every `self` subscriber."""
    pair = await sources_repo.get_user_source(session, user_id=user_id, source_id=source_id)
    if pair is None:
        raise SourceNotFoundError("Source not found.")
    source, user_source = pair
    if source.kind == "simkl":
        raise InvalidSourceOperationError("Use Sync now to refresh Simkl.")
    if source.kind != "lb_diary_rss":
        raise InvalidSourceOperationError(
            "Only RSS sources can be refreshed; re-upload a ZIP for exports."
        )
    try:
        feed = await preview_letterboxd_source(source.external_key, lb_client=lb_client)
    except LetterboxdError as exc:
        # The route turns this into an error response, which rolls back the request's session.
        await record_source_error(session_factory, source_id=source.id, error=str(exc))
        raise
    updated = await ingest_rss_feed(session, source=source, feed=feed, settings=settings)
    return SourceSummary(source=updated, user_source=user_source)


async def _poll_source(
    session_factory: async_sessionmaker[AsyncSession],
    client: LetterboxdClient,
    *,
    source_id: UUID,
    username: str,
    settings: Settings,
) -> None:
    try:
        feed = await client.fetch_rss(username)
        async with session_factory() as session:
            source = await sources_repo.get_source_by_id(session, source_id)
            if source is None:
                return  # deleted since it was claimed
            await ingest_rss_feed(session, source=source, feed=feed, settings=settings)
            await session.commit()
    except LetterboxdError as exc:
        error = str(exc)
    except Exception:
        log.exception("rss poll of source %s failed", source_id)
        error = "Unexpected error while refreshing. See worker logs."
    else:
        return
    await record_source_error(session_factory, source_id=source_id, error=error)


async def poll_due_rss_sources(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    max_sources: int,
    lb_client: LetterboxdClient | None = None,
) -> int:
    """Refresh up to `max_sources` due public RSS sources. Returns how many were claimed.

    A source is due when `next_refresh_at` is unset or past and it has a subscriber. Claiming one
    pushes its `next_refresh_at` out and commits before any network call, so concurrent workers
    take different sources and a failing feed is retried after the interval, not on every loop.
    """
    client = lb_client
    claimed = 0
    try:
        while claimed < max_sources:
            async with session_factory() as session:
                source = await sources_repo.claim_due_rss_source(
                    session, next_refresh_at=_next_refresh_at(settings)
                )
                if source is None:
                    break
                source_id, username = source.id, source.external_key
                await session.commit()
            claimed += 1
            client = client or LetterboxdClient()
            await _poll_source(
                session_factory, client, source_id=source_id, username=username, settings=settings
            )
    finally:
        if client is not None and lb_client is None:
            await client.aclose()
    return claimed


# --- Letterboxd export import (worker) ----------------------------------------------------------


@dataclass(frozen=True)
class _ExportBatch:
    kind: str
    external_key: str
    label: str
    url: str | None
    rows: list[LetterboxdExportRow]


def _export_batches(archive: LetterboxdExportArchive) -> list[_ExportBatch]:
    """The sources an export holds. Keys name the file, never the account an upload claims."""
    batches: list[_ExportBatch] = []
    if archive.profile_rows:
        claimed = archive.username
        batches.append(
            _ExportBatch(
                kind="lb_profile_csv",
                external_key="profile",
                label=f"Letterboxd Export (@{claimed})" if claimed else "Letterboxd Profile Export",
                url=f"https://letterboxd.com/{claimed}/" if claimed else None,
                rows=archive.profile_rows,
            )
        )
    for lst in archive.lists:
        batches.append(
            _ExportBatch(
                kind="lb_list_csv",
                external_key=f"list:{lst.slug}",
                label=lst.name,
                url=lst.url,
                rows=lst.rows,
            )
        )
    return batches


async def import_export_archive(
    session: AsyncSession,
    *,
    user_id: UUID,
    archive: LetterboxdExportArchive,
    role: str,
    client: TmdbClient,
    settings: Settings,
    emit_step: EmitStep | None = None,
) -> ImportResult:
    """Match an export's rows through TMDB and replace the user's export sources with them.

    Each source is keyed by `(kind, user, file)`, so an upload never writes to anyone else's
    source whatever username it claims. Re-importing replaces a source's items: titles that left
    the export are removed, and so are their ratings. The user's earlier import-issue
    resolutions are kept and applied again. The caller commits.
    """

    async def step(name: str, message: str, payload: dict[str, Any] | None = None) -> None:
        if emit_step is not None:
            await emit_step(name, message, payload)

    batches = _export_batches(archive)
    all_rows = [row for batch in batches for row in batch.rows]
    await step("match", f"Matching {len(all_rows)} rows against TMDB")
    matches = await _match_rows(client, all_rows)

    refs = list(
        {
            (m.ref.media_type, m.ref.tmdb_id): m.ref for m in matches.values() if m.ref is not None
        }.values()
    )
    await step("hydrate", f"Fetching details for {len(refs)} titles")
    try:
        await media_service.hydrate_titles(session, client, refs, settings=settings)
    except Exception:
        # Titles fetched before the failure are worth keeping; the import itself is not written.
        await session.commit()
        raise
    await session.commit()
    await sources_repo.ensure_media_item_stubs(
        session,
        [
            {"media_type": r.media_type, "tmdb_id": r.tmdb_id, "name": r.name or "", "year": r.year}
            for r in refs
        ],
    )

    await step("save", "Saving sources")
    matched = issues = skipped = 0
    summaries: list[SourceSummary] = []
    for batch in batches:
        source = await sources_repo.get_or_create_source(
            session,
            kind=batch.kind,
            owner_user_id=user_id,
            external_key=batch.external_key,
            label=batch.label,
            url=batch.url,
        )
        user_source = await subscribe_user_to_source(
            session, user_id=user_id, source=source, role=role
        )
        item_rows: list[dict[str, object]] = []
        issue_ids: list[UUID] = []
        for row in batch.rows:
            match = matches[_match_key(row)]
            target: tuple[str, int] | None = None
            if match.ref is not None:
                target = (match.ref.media_type, match.ref.tmdb_id)
                matched += 1
            else:
                issue, is_pending = await sources_repo.upsert_import_issue(
                    session,
                    user_id=user_id,
                    source_id=source.id,
                    item_kind=row.item_kind,
                    raw_title=row.title,
                    raw_year=row.year,
                    letterboxd_url=row.letterboxd_url,
                    rating=row.rating,
                    watched_at=row.watched_at,
                    confidence=match.confidence,
                    candidate_matches=match.candidates,
                )
                issue_ids.append(issue.id)
                if is_pending:
                    issues += 1
                else:
                    skipped += 1
                    if issue.status == "resolved" and issue.resolved_tmdb_id is not None:
                        target = (str(issue.resolved_media_type), issue.resolved_tmdb_id)
            if target is not None:
                item_rows.append(
                    {
                        "source_id": source.id,
                        "media_type": target[0],
                        "tmdb_id": target[1],
                        "item_kind": row.item_kind,
                        "rating": row.rating,
                        "watched_at": row.watched_at,
                        "entry_url": row.letterboxd_url,
                    }
                )
        await sources_repo.replace_source_items(
            session, source_id=source.id, rows=_merge_item_rows(item_rows)
        )
        await sources_repo.delete_pending_import_issues_except(
            session, user_id=user_id, source_id=source.id, keep_issue_ids=issue_ids
        )
        updated = await sources_repo.update_source_refresh_state(
            session,
            source_id=source.id,
            item_count=await sources_repo.count_source_items(session, source_id=source.id),
            last_refreshed_at=datetime.now(UTC),
            last_error=None,
        )
        summaries.append(SourceSummary(source=updated, user_source=user_source))
    await resync_watch_history(session, user_id=user_id)
    return ImportResult(
        sources=summaries, matched_count=matched, issue_count=issues, skipped_count=skipped
    )


async def discard_upload(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    upload_id: UUID,
) -> None:
    """Delete an uploaded ZIP on its own session, whatever happened to the job's session."""
    async with session_factory() as session:
        await sources_repo.delete_upload(session, user_id=user_id, upload_id=upload_id)
        await session.commit()


def lb_import_job_input(job: Job) -> tuple[UUID, str]:
    """The `(upload_id, role)` an `lb_import` job was enqueued with."""
    payload = job.payload or {}
    try:
        upload_id = UUID(str(payload["upload_id"]))
    except (KeyError, ValueError):
        raise ImportJobInputError("The import job has no upload. Upload the file again.") from None
    role = payload.get("role")
    if role not in IMPORT_ROLES:
        raise ImportJobInputError("The import job has an invalid role. Upload the file again.")
    return upload_id, str(role)


async def run_lb_import_job(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    rate_limiter: RateLimiter,
    job: Job,
    emit_step: EmitStep,
) -> None:
    """Handler for `lb_import` jobs: parse the stored ZIP, match it, and replace the sources.

    The upload is deleted together with the import's writes. When the job fails for good, the
    caller (`services.jobs`) discards it with `discard_upload`.
    """
    upload_id, role = lb_import_job_input(job)
    upload = await sources_repo.get_upload(session, user_id=job.user_id, upload_id=upload_id)
    if upload is None:
        raise ImportJobInputError("The uploaded file is no longer available. Upload it again.")
    filename, data = upload.filename, upload.data
    await emit_step("parse", f"Reading {filename}", None)
    # Parsing decompresses up to the archive limits; keep it off the loop that renews the lease.
    archive = await asyncio.to_thread(parse_export_zip, data)
    key = await keys_service.get_tmdb_key(
        session,
        audit_session_factory=session_factory,
        user_id=job.user_id,
        job_id=str(job.id),
        reason="letterboxd import",
        private_key=settings.key_private_key,
    )
    async with TmdbClient(key, rate_limiter) as client:
        result = await import_export_archive(
            session,
            user_id=job.user_id,
            archive=archive,
            role=role,
            client=client,
            settings=settings,
            emit_step=emit_step,
        )
    await sources_repo.delete_upload(session, user_id=job.user_id, upload_id=upload_id)
    await emit_step(
        "import",
        f"Imported {result.matched_count} titles; {result.issue_count} to review.",
        {
            "matched_count": result.matched_count,
            "issue_count": result.issue_count,
            "skipped_count": result.skipped_count,
            "source_count": len(result.sources),
        },
    )


# --- Import issues ------------------------------------------------------------------------------


async def list_import_issues(
    session: AsyncSession,
    *,
    user_id: UUID,
    status: str | None = None,
) -> tuple[list[ImportIssue], int]:
    issues = await sources_repo.list_import_issues(session, user_id=user_id, status=status)
    pending_count = await sources_repo.count_pending_import_issues(session, user_id=user_id)
    return (issues, pending_count)


def _stub_for_choice(issue: ImportIssue, media_type: str, tmdb_id: int) -> dict[str, object]:
    """A `media_items` stub for the title a user picked. No TMDB call; hydration comes later."""
    for candidate in issue.candidate_matches or []:
        if candidate.get("media_type") == media_type and candidate.get("tmdb_id") == tmdb_id:
            return {
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "name": str(candidate.get("name") or issue.raw_title),
                "year": candidate.get("year"),
            }
    return {
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "name": issue.raw_title,
        "year": issue.raw_year,
    }


async def resolve_import_issue(
    session: AsyncSession,
    *,
    user_id: UUID,
    issue_id: UUID,
    media_type: MediaType,
    tmdb_id: int,
) -> ImportIssue:
    """Resolve a pending issue to the chosen TMDB title, without calling TMDB."""
    issue = await sources_repo.get_import_issue(session, user_id=user_id, issue_id=issue_id)
    if issue is None:
        raise ImportIssueNotFoundError("Import issue not found.")
    if issue.status != "pending":
        raise InvalidSourceOperationError(f"Import issue is already {issue.status}.")
    await sources_repo.ensure_media_item_stubs(
        session, [_stub_for_choice(issue, media_type, tmdb_id)]
    )
    await sources_repo.upsert_source_items(
        session,
        [
            {
                "source_id": issue.source_id,
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "item_kind": issue.item_kind,
                "rating": issue.rating,
                "watched_at": issue.watched_at,
                "entry_url": issue.letterboxd_url,
            }
        ],
    )
    await sources_repo.set_source_item_count(
        session,
        source_id=issue.source_id,
        item_count=await sources_repo.count_source_items(session, source_id=issue.source_id),
    )
    if issue.item_kind in _WATCH_SIGNAL_KINDS:
        await resync_watch_history(session, user_id=user_id)
    resolved = await sources_repo.resolve_import_issue(
        session, user_id=user_id, issue_id=issue_id, media_type=media_type, tmdb_id=tmdb_id
    )
    assert resolved is not None
    return resolved


async def dismiss_import_issue(
    session: AsyncSession,
    *,
    user_id: UUID,
    issue_id: UUID,
) -> ImportIssue:
    dismissed = await sources_repo.dismiss_import_issue(session, user_id=user_id, issue_id=issue_id)
    if dismissed is None:
        raise ImportIssueNotFoundError("Import issue not found.")
    return dismissed
