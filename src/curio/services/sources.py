"""Typed taste source management, Letterboxd RSS polling, and ZIP export import."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.letterboxd import (
    LetterboxdClient,
    LetterboxdError,
    LetterboxdExportRow,
    LetterboxdRssFeed,
    normalize_letterboxd_username,
    parse_export_zip,
    rss_url_for_username,
)
from curio.clients.tmdb import MediaType, TmdbClient, TmdbRef
from curio.crypto import PrivateKeyNotConfiguredError
from curio.db.models.sources import ImportIssue, Source, UserSource
from curio.repositories import sources as sources_repo
from curio.services import keys as keys_service
from curio.services import media as media_service
from curio.settings import Settings

HIGH_CONFIDENCE_THRESHOLD = 0.85
MAX_CANDIDATE_MATCHES = 5
_WATCH_SIGNAL_KINDS = frozenset({"watched", "rating", "diary"})
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class SourceNotFoundError(LookupError):
    """Raised when a source or user-source subscription does not exist."""


class ImportIssueNotFoundError(LookupError):
    """Raised when an import issue does not exist for the user."""


class InvalidSourceOperationError(ValueError):
    """Raised when an operation is invalid for the source kind or target."""


@dataclass(frozen=True)
class SourceSummary:
    source: Source
    user_source: UserSource


@dataclass(frozen=True)
class ZipImportResult:
    sources: list[SourceSummary]
    matched_count: int
    issue_count: int
    skipped_count: int


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


def _scale_rating_for_watch_history(lb_rating: float | None) -> float | None:
    """Convert Letterboxd 0.5-5.0 star ratings to Curio's 0-10 watch_history scale."""
    if lb_rating is None:
        return None
    return round(lb_rating * 2.0, 2)


def _dedupe_source_item_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[tuple[object, object, object, object], dict[str, object]] = {}
    for row in rows:
        key = (row["source_id"], row["media_type"], row["tmdb_id"], row["item_kind"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
        else:
            if row.get("rating") is not None:
                existing["rating"] = row["rating"]
            if row.get("watched_at") is not None:
                existing["watched_at"] = row["watched_at"]
            if row.get("entry_url") is not None:
                existing["entry_url"] = row["entry_url"]
    return list(merged.values())


def _dedupe_watch_history_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[tuple[object, object], dict[str, object]] = {}
    for row in rows:
        key = (row["media_type"], row["tmdb_id"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
        else:
            if row.get("rating") is not None:
                existing["rating"] = row["rating"]
            if row.get("watched_at") is not None:
                existing["watched_at"] = row["watched_at"]
    return list(merged.values())


async def _sync_watch_history_for_source(
    session: AsyncSession,
    *,
    source_id: UUID,
    user_ids: list[UUID],
) -> None:
    if not user_ids:
        return
    signals = await sources_repo.list_source_watch_signals(session, source_id=source_id)
    if not signals:
        return
    raw_rows: list[dict[str, object]] = [
        {
            "media_type": item.media_type,
            "tmdb_id": item.tmdb_id,
            "status": "watched",
            "rating": _scale_rating_for_watch_history(item.rating),
            "watched_at": item.watched_at,
            "source": "letterboxd",
        }
        for item in signals
    ]
    deduped = _dedupe_watch_history_rows(raw_rows)
    for uid in user_ids:
        await sources_repo.upsert_watch_history_from_source(session, user_id=uid, rows=deduped)


async def _optional_tmdb_client(
    session: AsyncSession,
    *,
    audit_session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    settings: Settings,
) -> TmdbClient | None:
    try:
        return await media_service.tmdb_client_for_user(
            session,
            audit_session_factory=audit_session_factory,
            user_id=user_id,
            job_id=None,
            settings=settings,
            limiter=media_service.build_rate_limiter(settings),
        )
    except (keys_service.KeyNotFoundError, PrivateKeyNotConfiguredError):
        return None


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


async def list_user_sources(session: AsyncSession, *, user_id: UUID) -> list[SourceSummary]:
    rows = await sources_repo.list_user_sources(session, user_id=user_id)
    return [SourceSummary(source=s, user_source=us) for s, us in rows]


async def _ingest_rss_feed(
    session: AsyncSession,
    *,
    source: Source,
    feed: LetterboxdRssFeed,
    settings: Settings,
    tmdb_client: TmdbClient | None,
) -> Source:
    refs: list[TmdbRef] = []
    stub_rows: list[dict[str, object]] = []
    item_rows: list[dict[str, object]] = []

    for entry in feed.entries:
        if entry.tmdb_id is None:
            continue
        refs.append(
            TmdbRef(
                media_type=entry.media_type,
                tmdb_id=entry.tmdb_id,
                name=entry.title,
                year=entry.year,
            )
        )
        stub_rows.append(
            {
                "media_type": entry.media_type,
                "tmdb_id": entry.tmdb_id,
                "name": entry.title,
                "year": entry.year,
            }
        )
        item_rows.append(
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

    if refs:
        if tmdb_client is not None:
            await media_service.hydrate_titles(session, tmdb_client, refs, settings=settings)
        else:
            await sources_repo.ensure_media_item_stubs(session, stub_rows)

    deduped_items = _dedupe_source_item_rows(item_rows)
    await sources_repo.upsert_source_items(session, deduped_items)

    item_count = await sources_repo.count_source_items(session, source_id=source.id)
    updated_source = await sources_repo.update_source_refresh_state(
        session,
        source_id=source.id,
        item_count=item_count,
        last_refreshed_at=datetime.now(UTC),
        last_error=None,
    )

    self_user_ids = await sources_repo.list_self_user_ids_for_source(session, source_id=source.id)
    await _sync_watch_history_for_source(session, source_id=source.id, user_ids=self_user_ids)
    return updated_source


async def add_rss_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    input_value: str,
    role: str,
    settings: Settings,
    audit_session_factory: async_sessionmaker[AsyncSession],
    lb_client: LetterboxdClient | None = None,
    tmdb_client: TmdbClient | None = None,
) -> SourceSummary:
    """Subscribe `user_id` to a shared Letterboxd diary RSS source and ingest its entries."""
    username = normalize_letterboxd_username(input_value)
    feed = await preview_letterboxd_source(username, lb_client=lb_client)
    source = await sources_repo.get_or_create_source(
        session,
        kind="lb_diary_rss",
        external_key=username,
        label=f"Letterboxd RSS (@{username})",
        url=rss_url_for_username(username),
    )
    user_source = await sources_repo.upsert_user_source(
        session,
        user_id=user_id,
        source_id=source.id,
        role=role,
    )

    owns_tmdb = False
    client = tmdb_client
    if client is None:
        client = await _optional_tmdb_client(
            session,
            audit_session_factory=audit_session_factory,
            user_id=user_id,
            settings=settings,
        )
        owns_tmdb = client is not None

    try:
        updated_source = await _ingest_rss_feed(
            session,
            source=source,
            feed=feed,
            settings=settings,
            tmdb_client=client,
        )
    finally:
        if owns_tmdb and client is not None:
            await client.aclose()

    return SourceSummary(source=updated_source, user_source=user_source)


async def refresh_rss_source(
    session: AsyncSession,
    *,
    source_id: UUID,
    user_id: UUID | None = None,
    settings: Settings,
    audit_session_factory: async_sessionmaker[AsyncSession] | None = None,
    lb_client: LetterboxdClient | None = None,
    tmdb_client: TmdbClient | None = None,
) -> SourceSummary | Source:
    """Refresh a shared `lb_diary_rss` source once and sync all subscribed `self` users."""
    if user_id is not None:
        pair = await sources_repo.get_user_source(session, user_id=user_id, source_id=source_id)
        if pair is None:
            raise SourceNotFoundError("Source not found.")
        source, user_source = pair
    else:
        found = await sources_repo.get_source_by_id(session, source_id)
        if found is None:
            raise SourceNotFoundError("Source not found.")
        source = found
        user_source = None

    if source.kind != "lb_diary_rss":
        raise InvalidSourceOperationError(
            "Only RSS sources can be refreshed; re-upload a ZIP for exports."
        )

    try:
        feed = await preview_letterboxd_source(source.external_key, lb_client=lb_client)
    except LetterboxdError as exc:
        await sources_repo.update_source_refresh_state(
            session,
            source_id=source.id,
            item_count=source.item_count,
            last_error=str(exc),
        )
        raise

    owns_tmdb = False
    client = tmdb_client
    if client is None and user_id is not None and audit_session_factory is not None:
        client = await _optional_tmdb_client(
            session,
            audit_session_factory=audit_session_factory,
            user_id=user_id,
            settings=settings,
        )
        owns_tmdb = client is not None

    try:
        updated_source = await _ingest_rss_feed(
            session,
            source=source,
            feed=feed,
            settings=settings,
            tmdb_client=client,
        )
    finally:
        if owns_tmdb and client is not None:
            await client.aclose()

    if user_source is not None:
        return SourceSummary(source=updated_source, user_source=user_source)
    return updated_source


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
    updated_us = await sources_repo.update_user_source_role(
        session,
        user_id=user_id,
        source_id=source_id,
        role=role,
    )
    if updated_us is None:
        raise SourceNotFoundError("Source not found.")
    if role == "self":
        await _sync_watch_history_for_source(session, source_id=source_id, user_ids=[user_id])
    return SourceSummary(source=source, user_source=updated_us)


async def remove_user_source(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_id: UUID,
) -> None:
    deleted = await sources_repo.delete_user_source(session, user_id=user_id, source_id=source_id)
    if not deleted:
        raise SourceNotFoundError("Source not found.")


async def _match_export_row(
    tmdb_client: TmdbClient,
    row: LetterboxdExportRow,
    cache: dict[tuple[str, int | None], list[tuple[TmdbRef, float]]],
) -> tuple[TmdbRef | None, float, list[dict[str, Any]]]:
    key = (_normalize_title(row.title), row.year)
    scored = cache.get(key)
    if scored is None:
        refs = await tmdb_client.search("movie", row.title, row.year)
        if not refs and row.year is not None:
            # Retry without year so near-year releases still show up as candidates.
            refs = await tmdb_client.search("movie", row.title)
        scored = sorted(
            [(ref, _score_candidate(row.title, row.year, ref)) for ref in refs],
            key=lambda pair: pair[1],
            reverse=True,
        )
        cache[key] = scored

    candidates_payload: list[dict[str, Any]] = [
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
        return (None, 0.0, candidates_payload)

    best_ref, best_score = scored[0]
    if best_score < HIGH_CONFIDENCE_THRESHOLD:
        return (None, best_score, candidates_payload)

    # Ambiguous tie at the top goes to manual review rather than guessing.
    if len(scored) > 1 and scored[1][1] == best_score:
        return (None, best_score, candidates_payload)

    return (best_ref, best_score, candidates_payload)


async def import_zip_sources(
    session: AsyncSession,
    *,
    user_id: UUID,
    zip_bytes: bytes,
    role: str,
    settings: Settings,
    audit_session_factory: async_sessionmaker[AsyncSession],
    tmdb_client: TmdbClient | None = None,
) -> ZipImportResult:
    """Parse a Letterboxd export ZIP, match rows via TMDB, and persist sources/items/issues."""
    archive = parse_export_zip(zip_bytes)

    owns_tmdb = False
    client = tmdb_client
    if client is None:
        client = await media_service.tmdb_client_for_user(
            session,
            audit_session_factory=audit_session_factory,
            user_id=user_id,
            job_id=None,
            settings=settings,
            limiter=media_service.build_rate_limiter(settings),
        )
        owns_tmdb = True

    owner_key = archive.username or f"user:{user_id}"
    search_cache: dict[tuple[str, int | None], list[tuple[TmdbRef, float]]] = {}
    matched_count = 0
    issue_count = 0
    skipped_count = 0
    summaries: list[SourceSummary] = []

    try:
        batches: list[tuple[str, str, str, str | None, list[LetterboxdExportRow]]] = []
        if archive.profile_rows:
            label = (
                f"Letterboxd Export (@{archive.username})"
                if archive.username
                else "Letterboxd Profile Export"
            )
            url = f"https://letterboxd.com/{archive.username}/" if archive.username else None
            batches.append(("lb_profile_csv", owner_key, label, url, archive.profile_rows))

        for lst in archive.lists:
            batches.append(
                (
                    "lb_list_csv",
                    f"{owner_key}:{lst.slug}",
                    lst.name,
                    lst.url,
                    lst.rows,
                )
            )

        for kind, ext_key, label, url, rows in batches:
            source = await sources_repo.get_or_create_source(
                session,
                kind=kind,
                external_key=ext_key,
                label=label,
                url=url,
            )
            user_source = await sources_repo.upsert_user_source(
                session,
                user_id=user_id,
                source_id=source.id,
                role=role,
            )

            matched_refs: list[TmdbRef] = []
            item_rows: list[dict[str, object]] = []

            for row in rows:
                best_ref, confidence, candidates = await _match_export_row(
                    client, row, search_cache
                )
                if best_ref is not None:
                    matched_refs.append(best_ref)
                    item_rows.append(
                        {
                            "source_id": source.id,
                            "media_type": best_ref.media_type,
                            "tmdb_id": best_ref.tmdb_id,
                            "item_kind": row.item_kind,
                            "rating": row.rating,
                            "watched_at": row.watched_at,
                            "entry_url": row.letterboxd_url,
                        }
                    )
                    matched_count += 1
                else:
                    _, is_pending = await sources_repo.upsert_import_issue(
                        session,
                        user_id=user_id,
                        source_id=source.id,
                        item_kind=row.item_kind,
                        raw_title=row.title,
                        raw_year=row.year,
                        letterboxd_url=row.letterboxd_url,
                        rating=row.rating,
                        watched_at=row.watched_at,
                        confidence=confidence,
                        candidate_matches=candidates,
                    )
                    if is_pending:
                        issue_count += 1
                    else:
                        skipped_count += 1

            if matched_refs:
                await media_service.hydrate_titles(session, client, matched_refs, settings=settings)
                await sources_repo.upsert_source_items(session, _dedupe_source_item_rows(item_rows))

            item_count = await sources_repo.count_source_items(session, source_id=source.id)
            updated_source = await sources_repo.update_source_refresh_state(
                session,
                source_id=source.id,
                item_count=item_count,
                last_refreshed_at=datetime.now(UTC),
                last_error=None,
            )

            if role == "self":
                await _sync_watch_history_for_source(
                    session, source_id=source.id, user_ids=[user_id]
                )

            summaries.append(SourceSummary(source=updated_source, user_source=user_source))
    finally:
        if owns_tmdb:
            await client.aclose()

    return ZipImportResult(
        sources=summaries,
        matched_count=matched_count,
        issue_count=issue_count,
        skipped_count=skipped_count,
    )


async def list_import_issues(
    session: AsyncSession,
    *,
    user_id: UUID,
    status: str | None = None,
) -> tuple[list[ImportIssue], int]:
    issues = await sources_repo.list_import_issues(session, user_id=user_id, status=status)
    pending_count = await sources_repo.count_pending_import_issues(session, user_id=user_id)
    return (issues, pending_count)


async def resolve_import_issue(
    session: AsyncSession,
    *,
    user_id: UUID,
    issue_id: UUID,
    media_type: MediaType,
    tmdb_id: int,
    settings: Settings,
    audit_session_factory: async_sessionmaker[AsyncSession],
    tmdb_client: TmdbClient | None = None,
) -> ImportIssue:
    issue = await sources_repo.get_import_issue(session, user_id=user_id, issue_id=issue_id)
    if issue is None:
        raise ImportIssueNotFoundError("Import issue not found.")

    owns_tmdb = False
    client = tmdb_client
    if client is None:
        client = await media_service.tmdb_client_for_user(
            session,
            audit_session_factory=audit_session_factory,
            user_id=user_id,
            job_id=None,
            settings=settings,
            limiter=media_service.build_rate_limiter(settings),
        )
        owns_tmdb = True

    try:
        hydrated = await media_service.hydrate_titles(
            session,
            client,
            [TmdbRef(media_type=media_type, tmdb_id=tmdb_id)],
            settings=settings,
        )
    finally:
        if owns_tmdb:
            await client.aclose()

    if not hydrated:
        raise InvalidSourceOperationError(f"TMDB {media_type} {tmdb_id} was not found.")

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

    item_count = await sources_repo.count_source_items(session, source_id=issue.source_id)
    await sources_repo.update_source_refresh_state(
        session, source_id=issue.source_id, item_count=item_count
    )

    pair = await sources_repo.get_user_source(session, user_id=user_id, source_id=issue.source_id)
    if pair is not None and pair[1].role == "self" and issue.item_kind in _WATCH_SIGNAL_KINDS:
        await sources_repo.upsert_watch_history_from_source(
            session,
            user_id=user_id,
            rows=[
                {
                    "media_type": media_type,
                    "tmdb_id": tmdb_id,
                    "status": "watched",
                    "rating": _scale_rating_for_watch_history(issue.rating),
                    "watched_at": issue.watched_at,
                    "source": "letterboxd",
                }
            ],
        )

    resolved = await sources_repo.resolve_import_issue(
        session,
        user_id=user_id,
        issue_id=issue_id,
        media_type=media_type,
        tmdb_id=tmdb_id,
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
