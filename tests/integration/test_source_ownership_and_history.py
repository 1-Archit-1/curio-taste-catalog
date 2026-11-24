"""Source ownership (ADR 0009), watch history provenance and precedence, and re-import semantics."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from curio.clients.letterboxd import parse_export_zip
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import TmdbClient
from curio.db.models.catalogs import WatchHistory
from curio.db.models.sources import ImportIssue, Source, SourceItem, UserSource
from curio.repositories import sources as sources_repo
from curio.services import sources as sources_service
from curio.services.users import ProvisionedUser
from curio.settings import Settings
from tests.integration.lb_support import (
    TMDB_BASE,
    TMDB_FIXTURES,
    build_fixture_zip,
    build_zip,
    login,
    mock_tmdb_for_zip,
)

PROFILE_HEADER = "Date Joined,Username,Given Name,Family Name,Email Address\n"
WATCHED_HEADER = "Date,Name,Year,Letterboxd URI\n"
DIARY_HEADER = "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
RATINGS_HEADER = "Date,Name,Year,Letterboxd URI,Rating\n"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url="postgresql://unused")


@pytest.fixture
async def tmdb() -> AsyncIterator[TmdbClient]:
    async with TmdbClient("fake-tmdb-key", RateLimiter(1000.0)) as client:
        yield client


async def _import(
    session: AsyncSession,
    tmdb: TmdbClient,
    settings: Settings,
    user: ProvisionedUser,
    zip_bytes: bytes,
    role: str = "self",
) -> sources_service.ImportResult:
    return await sources_service.import_export_archive(
        session,
        user_id=user.user.id,
        archive=parse_export_zip(zip_bytes),
        role=role,
        client=tmdb,
        settings=settings,
    )


async def _history(session: AsyncSession, user: ProvisionedUser) -> dict[tuple[str, int], object]:
    # Writes use bulk statements that bypass the identity map, so reload what is already cached.
    rows = await session.scalars(
        select(WatchHistory)
        .where(WatchHistory.user_id == user.user.id)
        .execution_options(populate_existing=True)
    )
    return {(r.media_type, r.tmdb_id): r for r in rows}


async def _items(session: AsyncSession, source_id: UUID) -> set[tuple[str, int, str, float | None]]:
    rows = await session.scalars(
        select(SourceItem)
        .where(SourceItem.source_id == source_id)
        .execution_options(populate_existing=True)
    )
    return {(r.media_type, r.tmdb_id, r.item_kind, r.rating) for r in rows}


# --- Ownership ----------------------------------------------------------------------------------


async def test_uploading_an_export_that_claims_another_username_never_touches_their_source(
    client: httpx.AsyncClient,
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    tmdb: TmdbClient,
    settings: Settings,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    mock_tmdb_for_zip(respx_mock)
    alice = await make_committed_user("own_alice")
    bob = await make_committed_user("own_bob")
    alice_result = await _import(session, tmdb, settings, alice, build_fixture_zip())
    alice_profile = alice_result.sources[0].source
    assert alice_profile.owner_user_id == alice.user.id
    alice_items = await _items(session, alice_profile.id)
    alice_history = set(await _history(session, alice))

    # Bob's file says it belongs to alice, and holds one different row.
    bob_zip = build_zip(
        {
            "profile.csv": PROFILE_HEADER + "2022-01-10,alice,Bob,B,b@example.invalid\n",
            "watched.csv": WATCHED_HEADER
            + "2026-01-01,Terminator Genisys,2015,https://boxd.it/t\n",
        }
    )
    bob_result = await _import(session, tmdb, settings, bob, bob_zip, role="taste")

    bob_profile = bob_result.sources[0].source
    assert bob_profile.id != alice_profile.id
    assert bob_profile.owner_user_id == bob.user.id
    assert await _items(session, alice_profile.id) == alice_items
    assert set(await _history(session, alice)) == alice_history
    assert await _items(session, bob_profile.id) == {("movie", 87101, "watched", None)}
    links = await session.scalars(
        select(UserSource.user_id).where(UserSource.source_id == alice_profile.id)
    )
    assert list(links) == [alice.user.id]

    # Bob can't see, change, remove, refresh or subscribe to Alice's source.
    await login(client, bob.login_token)
    listed = await client.get("/api/sources")
    assert {s["id"] for s in listed.json()["sources"]} == {str(bob_profile.id)}
    missing = f"/api/sources/{alice_profile.id}"
    assert (await client.patch(missing, json={"role": "self"})).status_code == 404
    assert (await client.delete(missing)).status_code == 404
    assert (await client.post(f"{missing}/refresh")).status_code == 404
    with pytest.raises(sources_service.SourceNotFoundError):
        await sources_service.subscribe_user_to_source(
            session, user_id=bob.user.id, source=alice_profile, role="taste"
        )
    assert (
        await sources_repo.get_user_source(session, user_id=bob.user.id, source_id=alice_profile.id)
        is None
    )
    assert await _items(session, alice_profile.id) == alice_items


async def test_a_stray_subscription_to_an_owned_source_stays_invisible(
    session: AsyncSession,
    tmdb: TmdbClient,
    settings: Settings,
    respx_mock: respx.MockRouter,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    mock_tmdb_for_zip(respx_mock)
    alice = await make_committed_user("stray_alice")
    bob = await make_committed_user("stray_bob")
    result = await _import(session, tmdb, settings, alice, build_fixture_zip())
    source = result.sources[0].source
    session.add(UserSource(user_id=bob.user.id, source_id=source.id, role="self"))
    await session.flush()

    assert await sources_service.list_user_sources(session, user_id=bob.user.id) == []
    assert (
        await sources_repo.get_user_source(session, user_id=bob.user.id, source_id=source.id)
        is None
    )
    with pytest.raises(sources_service.SourceNotFoundError):
        await sources_service.update_source_role(
            session, user_id=bob.user.id, source_id=source.id, role="taste"
        )


async def test_export_sources_are_keyed_by_file_and_owner_not_by_username(
    session: AsyncSession,
    tmdb: TmdbClient,
    settings: Settings,
    respx_mock: respx.MockRouter,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    mock_tmdb_for_zip(respx_mock)
    alice = await make_committed_user("keyed_alice")
    first = await _import(session, tmdb, settings, alice, build_fixture_zip())
    renamed = await _import(session, tmdb, settings, alice, build_fixture_zip(username="renamed"))
    assert [s.source.id for s in renamed.sources] == [s.source.id for s in first.sources]
    sources = list(
        await session.scalars(select(Source).where(Source.owner_user_id == alice.user.id))
    )
    assert sorted((s.kind, s.external_key) for s in sources) == [
        ("lb_list_csv", "list:sci-fi-favorites"),
        ("lb_profile_csv", "profile"),
    ]


# --- Watch history provenance and precedence ----------------------------------------------------


async def _export_source(
    session: AsyncSession,
    user: ProvisionedUser,
    key: str,
    items: list[tuple[str, int, str, float | None]],
    role: str = "self",
) -> Source:
    """An owned export source with `(media_type, tmdb_id, item_kind, rating)` items."""
    source = await sources_repo.get_or_create_source(
        session,
        kind="lb_profile_csv",
        owner_user_id=user.user.id,
        external_key=key,
        label=key,
        url=None,
    )
    await sources_service.subscribe_user_to_source(
        session, user_id=user.user.id, source=source, role=role
    )
    await sources_repo.ensure_media_item_stubs(
        session,
        [{"media_type": m, "tmdb_id": t, "name": f"Title {t}", "year": 2000} for m, t, *_ in items],
    )
    await sources_repo.replace_source_items(
        session,
        source_id=source.id,
        rows=[
            {
                "source_id": source.id,
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "item_kind": kind,
                "rating": rating,
                "watched_at": None,
                "entry_url": None,
            }
            for media_type, tmdb_id, kind, rating in items
        ],
    )
    return source


async def test_a_second_self_source_never_overwrites_and_takes_over_when_the_first_goes(
    session: AsyncSession,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("precedence_user")
    uid = user.user.id
    first = await _export_source(session, user, "first", [("movie", 603, "rating", 8.0)])
    await sources_service.resync_watch_history(session, user_id=uid)
    # A manual row for a title both sources have.
    await sources_repo.ensure_media_item_stubs(
        session, [{"media_type": "series", "tmdb_id": 1396, "name": "Breaking Bad", "year": 2008}]
    )
    session.add(WatchHistory(user_id=uid, media_type="series", tmdb_id=1396, rating=7.0))
    await session.flush()

    second = await _export_source(
        session,
        user,
        "second",
        [
            ("movie", 603, "rating", 4.0),
            ("movie", 87101, "rating", 6.0),
            ("series", 1396, "rating", 3.0),
        ],
    )
    await sources_service.resync_watch_history(session, user_id=uid)

    rows = await _history(session, user)
    assert (rows[("movie", 603)].rating, rows[("movie", 603)].source_id) == (8.0, first.id)
    assert (rows[("movie", 87101)].rating, rows[("movie", 87101)].source_id) == (6.0, second.id)
    manual = rows[("series", 1396)]
    assert (manual.rating, manual.source_id, manual.source) == (7.0, None, "manual")

    # The owner's own changes propagate, even to a rating it dropped.
    await sources_repo.replace_source_items(
        session,
        source_id=first.id,
        rows=[
            {
                "source_id": first.id,
                "media_type": "movie",
                "tmdb_id": 603,
                "item_kind": "watched",
                "rating": None,
                "watched_at": None,
                "entry_url": None,
            }
        ],
    )
    await sources_service.resync_watch_history(session, user_id=uid)
    rows = await _history(session, user)
    assert (rows[("movie", 603)].rating, rows[("movie", 603)].source_id) == (None, first.id)

    # Removing the first source deletes its rows, then the second takes over the title.
    await sources_service.remove_user_source(session, user_id=uid, source_id=first.id)
    rows = await _history(session, user)
    assert (rows[("movie", 603)].rating, rows[("movie", 603)].source_id) == (4.0, second.id)
    assert rows[("series", 1396)].source_id is None
    assert await session.scalar(select(Source).where(Source.id == first.id)) is None

    # Leaving `self` removes what the source wrote and still never touches manual rows.
    await sources_service.update_source_role(
        session, user_id=uid, source_id=second.id, role="taste"
    )
    rows = await _history(session, user)
    assert set(rows) == {("series", 1396)}
    assert rows[("series", 1396)].rating == 7.0


async def test_a_source_that_drops_a_title_lets_the_other_source_claim_it(
    session: AsyncSession,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("handover_user")
    first = await _export_source(session, user, "first", [("movie", 603, "rating", 8.0)])
    second = await _export_source(session, user, "second", [("movie", 603, "rating", 4.0)])
    await sources_service.resync_watch_history(session, user_id=user.user.id)
    owners = {k: v.source_id for k, v in (await _history(session, user)).items()}
    assert owners[("movie", 603)] in {first.id, second.id}
    holder, other = (first, second) if owners[("movie", 603)] == first.id else (second, first)

    await sources_repo.replace_source_items(session, source_id=holder.id, rows=[])
    await sources_service.resync_watch_history(session, user_id=user.user.id)
    row = (await _history(session, user))[("movie", 603)]
    assert row.source_id == other.id


# --- Re-import semantics ------------------------------------------------------------------------


async def test_reimport_replaces_items_and_clears_removed_ratings(
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    tmdb: TmdbClient,
    settings: Settings,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    mock_tmdb_for_zip(respx_mock)
    user = await make_committed_user("reimport_user")
    first = await _import(session, tmdb, settings, user, build_fixture_zip())
    assert (first.matched_count, first.issue_count) == (5, 1)
    profile = first.sources[0].source
    assert first.sources[0].source.item_count == 4
    # ratings.csv (4.5 stars) wins over the diary entry (5 stars).
    row = (await _history(session, user))[("movie", 603)]
    assert (row.rating, row.source_id) == (9.0, profile.id)
    assert ("movie", 87101, "watchlist", None) in await _items(session, profile.id)

    # A newer export without ratings, diary, or watchlist.
    smaller = build_zip(
        {
            "profile.csv": (
                "Date Joined,Username,Given Name,Family Name,Email Address\n"
                "2022-01-10,alice,Alice,Reader,a@example.invalid\n"
            ),
            "watched.csv": WATCHED_HEADER + "2026-10-01,The Matrix,1999,https://boxd.it/m\n",
        }
    )
    second = await _import(session, tmdb, settings, user, smaller)
    assert second.sources[0].source.id == profile.id
    assert second.sources[0].source.item_count == 1
    assert await _items(session, profile.id) == {("movie", 603, "watched", None)}
    row = (await _history(session, user))[("movie", 603)]
    assert row.rating is None
    # The ambiguous row left the export, so its pending issue went too.
    pending = await session.scalars(select(ImportIssue).where(ImportIssue.user_id == user.user.id))
    assert list(pending) == []
    assert second.sources[0].source.last_refreshed_at is not None
    # The list source was not in the new export and is left alone.
    lists = await session.scalars(select(Source).where(Source.kind == "lb_list_csv"))
    assert [s.item_count for s in lists] == [1]


async def test_reimport_keeps_a_resolved_issue_and_never_duplicates(
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    tmdb: TmdbClient,
    settings: Settings,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    mock_tmdb_for_zip(respx_mock)
    user = await make_committed_user("resolved_keep_user")
    zip_bytes = build_fixture_zip()
    first = await _import(session, tmdb, settings, user, zip_bytes)
    items_before = await _items(session, first.sources[0].source.id)
    issues, pending = await sources_service.list_import_issues(session, user_id=user.user.id)
    assert pending == 1
    await sources_service.resolve_import_issue(
        session, user_id=user.user.id, issue_id=issues[0].id, media_type="movie", tmdb_id=87101
    )

    again = await _import(session, tmdb, settings, user, zip_bytes)
    assert (again.matched_count, again.issue_count, again.skipped_count) == (5, 0, 1)
    items_after = await _items(session, again.sources[0].source.id)
    assert items_after == items_before | {("movie", 87101, "watched", None)}
    assert ("movie", 87101) in await _history(session, user)

    once_more = await _import(session, tmdb, settings, user, zip_bytes)
    assert await _items(session, once_more.sources[0].source.id) == items_after


async def test_dismissed_rows_stay_out_across_reimports(
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    tmdb: TmdbClient,
    settings: Settings,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    mock_tmdb_for_zip(respx_mock)
    user = await make_committed_user("dismissed_user")
    zip_bytes = build_fixture_zip()
    first = await _import(session, tmdb, settings, user, zip_bytes, role="taste")
    issues, _ = await sources_service.list_import_issues(session, user_id=user.user.id)
    await sources_service.dismiss_import_issue(session, user_id=user.user.id, issue_id=issues[0].id)
    items_before = await _items(session, first.sources[0].source.id)
    again = await _import(session, tmdb, settings, user, zip_bytes, role="taste")
    assert (again.issue_count, again.skipped_count) == (0, 1)
    assert await _items(session, first.sources[0].source.id) == items_before
    assert await sources_service.list_import_issues(
        session, user_id=user.user.id, status="pending"
    ) == ([], 0)
    # `taste` sources never write watch history.
    assert await _history(session, user) == {}


async def test_diary_rating_comes_from_the_latest_watched_entry(
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    tmdb: TmdbClient,
    settings: Settings,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    mock_tmdb_for_zip(respx_mock)
    user = await make_committed_user("diary_latest_user")
    uri = "https://boxd.it/m"
    diary = (
        DIARY_HEADER
        # The newest entry comes first in the file, the oldest last.
        + f"2026-06-02,The Matrix,1999,{uri},3.0,Yes,,2026-06-01\n"
        + f"2026-01-02,The Matrix,1999,{uri},5.0,No,,2026-01-01\n"
    )
    files = {"profile.csv": PROFILE_HEADER + "2022-01-10,alice,A,B,a@example.invalid\n"}
    await _import(session, tmdb, settings, user, build_zip({**files, "diary.csv": diary}))
    row = (await _history(session, user))[("movie", 603)]
    assert row.rating == 6.0
    assert row.watched_at == datetime(2026, 6, 1, tzinfo=UTC)

    # A newer diary entry without a rating keeps the last rating given; ratings.csv beats both.
    unrated = diary + f"2026-07-02,The Matrix,1999,{uri},,Yes,,2026-07-01\n"
    await _import(session, tmdb, settings, user, build_zip({**files, "diary.csv": unrated}))
    row = (await _history(session, user))[("movie", 603)]
    assert (row.rating, row.watched_at) == (6.0, datetime(2026, 7, 1, tzinfo=UTC))

    ratings = RATINGS_HEADER + f"2026-08-01,The Matrix,1999,{uri},2.0\n"
    await _import(
        session,
        tmdb,
        settings,
        user,
        build_zip({**files, "diary.csv": diary, "ratings.csv": ratings}),
    )
    assert (await _history(session, user))[("movie", 603)].rating == 4.0


def test_merge_item_rows_keeps_the_latest_value_per_field():
    def row(day: int | None, rating: float | None, url: str | None) -> dict[str, object]:
        return {
            "source_id": "s",
            "media_type": "movie",
            "tmdb_id": 1,
            "item_kind": "diary",
            "rating": rating,
            "watched_at": datetime(2026, 1, day, tzinfo=UTC) if day else None,
            "entry_url": url,
        }

    merged = sources_service._merge_item_rows(
        [row(5, 8.0, "new"), row(1, 4.0, "old"), row(9, None, None), row(None, 2.0, "undated")]
    )
    assert len(merged) == 1
    assert merged[0]["rating"] == 8.0
    assert merged[0]["watched_at"] == datetime(2026, 1, 9, tzinfo=UTC)
    assert merged[0]["entry_url"] == "new"


# --- Matching -----------------------------------------------------------------------------------


async def test_titles_without_a_movie_match_are_matched_against_tv(
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    tmdb: TmdbClient,
    settings: Settings,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("tv_fallback_user")
    search_movie = respx_mock.get(f"{TMDB_BASE}/search/movie").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "results": (
                    [{"id": 603, "title": "The Matrix", "release_date": "1999-03-31"}]
                    if "matrix" in request.url.params["query"].lower()
                    else [{"id": 9, "title": "Breaking Dawn", "release_date": "2011-11-16"}]
                )
            },
        )
    )
    search_tv = respx_mock.get(f"{TMDB_BASE}/search/tv").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20"}]
            },
        )
    )
    respx_mock.get(f"{TMDB_BASE}/movie/603").mock(
        return_value=httpx.Response(
            200, text=(TMDB_FIXTURES / "movie_603.json").read_text(encoding="utf-8")
        )
    )
    respx_mock.get(f"{TMDB_BASE}/tv/1396").mock(
        return_value=httpx.Response(
            200, text=(TMDB_FIXTURES / "tv_1396.json").read_text(encoding="utf-8")
        )
    )
    zip_bytes = build_zip(
        {
            "watched.csv": WATCHED_HEADER
            + "2026-01-01,The Matrix,1999,https://boxd.it/m\n"
            + "2026-01-02,Breaking Bad,2008,https://boxd.it/b\n",
        }
    )
    result = await _import(session, tmdb, settings, user, zip_bytes)

    assert (result.matched_count, result.issue_count) == (2, 0)
    items = await _items(session, result.sources[0].source.id)
    assert items == {("movie", 603, "watched", None), ("series", 1396, "watched", None)}
    # A confident movie match never searches TV for that title.
    tv_queries = [c.request.url.params["query"] for c in search_tv.calls]
    assert tv_queries == ["Breaking Bad"]
    assert any("Matrix" in c.request.url.params["query"] for c in search_movie.calls)


async def test_an_ambiguous_tv_match_goes_to_review(
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    tmdb: TmdbClient,
    settings: Settings,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("tv_review_user")
    respx_mock.get(f"{TMDB_BASE}/search/movie").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx_mock.get(f"{TMDB_BASE}/search/tv").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 1, "name": "Lost Boys", "first_air_date": "2003-01-01"},
                    {"id": 2, "name": "Lost Boys", "first_air_date": "2003-06-01"},
                ]
            },
        )
    )
    zip_bytes = build_zip(
        {"watched.csv": WATCHED_HEADER + "2026-01-01,Lost Boys,2003,https://boxd.it/l\n"}
    )
    result = await _import(session, tmdb, settings, user, zip_bytes)
    assert (result.matched_count, result.issue_count) == (0, 1)
    issue = await session.scalar(select(ImportIssue).where(ImportIssue.user_id == user.user.id))
    assert issue is not None
    assert {(c["media_type"], c["tmdb_id"]) for c in issue.candidate_matches} == {
        ("series", 1),
        ("series", 2),
    }
