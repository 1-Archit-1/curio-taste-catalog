"""Integration tests for Issue #5: typed sources, public diary RSS, and import issues over HTTP."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.catalogs import MediaItem, WatchHistory
from curio.db.models.sources import ImportIssue, Source, SourceItem, UserSource
from curio.repositories import sources as sources_repo
from curio.services import sources as sources_service
from curio.services.users import ProvisionedUser
from tests.integration.lb_support import LB_FIXTURES, RSS_URL, login


async def test_sources_reject_unauthenticated_requests(client: httpx.AsyncClient):
    assert (await client.get("/api/sources")).status_code == 401
    assert (await client.post("/api/sources/preview", json={"input": "alice"})).status_code == 401
    assert (await client.get("/api/import-issues")).status_code == 401
    zip_resp = await client.post(
        "/api/sources/import-zip", files={"file": ("export.zip", b"PK", "application/zip")}
    )
    assert zip_resp.status_code == 401


async def test_rss_preview_add_shared_refresh_role_and_remove(
    client: httpx.AsyncClient,
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    """RSS needs no TMDB key (none exists here). Titles get stubs; history rows carry the source."""
    rss_xml = (LB_FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    rss_route = respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text=rss_xml))
    alice = await make_committed_user("rss_alice")
    bob = await make_committed_user("rss_bob")
    await login(client, alice.login_token)

    prev_resp = await client.post(
        "/api/sources/preview", json={"input": "https://letterboxd.com/alice/"}
    )
    assert prev_resp.status_code == 200
    prev_data = prev_resp.json()
    assert prev_data["username"] == "alice"
    assert prev_data["entry_count"] == 2
    assert prev_data["recent_titles"][0]["title"] == "The Matrix"
    assert prev_data["recent_titles"][0]["rating"] == 10.0

    add_resp = await client.post("/api/sources", json={"input": "alice", "role": "self"})
    assert add_resp.status_code == 201
    alice_source = add_resp.json()
    assert alice_source["kind"] == "lb_diary_rss"
    assert alice_source["role"] == "self"
    assert alice_source["item_count"] == 2
    assert alice_source["last_refreshed_at"] is not None
    source_id = UUID(alice_source["id"])

    stored = await session.scalar(select(Source).where(Source.id == source_id))
    assert stored is not None
    assert stored.owner_user_id is None
    assert stored.next_refresh_at is not None
    assert stored.next_refresh_at > datetime.now(UTC)
    matrix = await session.scalar(select(MediaItem).where(MediaItem.tmdb_id == 603))
    assert matrix is not None
    assert (matrix.name, matrix.year, matrix.hydrated_at) == ("The Matrix", 1999, None)

    wh_rows = list(
        await session.scalars(select(WatchHistory).where(WatchHistory.user_id == alice.user.id))
    )
    by_id = {(r.media_type, r.tmdb_id): r for r in wh_rows}
    assert set(by_id) == {("movie", 603), ("series", 1396)}
    assert by_id[("movie", 603)].rating == 10.0
    assert by_id[("movie", 603)].source == "letterboxd"
    assert by_id[("movie", 603)].source_id == source_id
    assert by_id[("series", 1396)].rating == 9.0

    # Bob subscribes to the same shared source as a friend. It is stored and fetched once.
    await login(client, bob.login_token)
    bob_add = await client.post(
        "/api/sources", json={"input": "https://letterboxd.com/alice/", "role": "taste"}
    )
    assert bob_add.status_code == 201
    assert bob_add.json()["id"] == alice_source["id"]
    assert await session.scalar(select(func.count()).select_from(Source)) == 1
    bob_history = select(func.count()).select_from(WatchHistory)
    bob_history = bob_history.where(WatchHistory.user_id == bob.user.id)
    assert await session.scalar(bob_history) == 0

    calls_before = rss_route.call_count
    refresh_resp = await client.post(f"/api/sources/{source_id}/refresh")
    assert refresh_resp.status_code == 200
    assert rss_route.call_count == calls_before + 1

    # Becoming `self` writes Bob's history; leaving `self` removes it again.
    patch_resp = await client.patch(f"/api/sources/{source_id}", json={"role": "self"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["role"] == "self"
    assert await session.scalar(bob_history) == 2
    await client.patch(f"/api/sources/{source_id}", json={"role": "taste"})
    assert await session.scalar(bob_history) == 0
    await client.patch(f"/api/sources/{source_id}", json={"role": "self"})
    assert await session.scalar(bob_history) == 2

    # Unsubscribing removes what the source wrote for Bob; Alice and the source stay.
    del_resp = await client.delete(f"/api/sources/{source_id}")
    assert del_resp.status_code == 200
    assert await session.scalar(bob_history) == 0
    assert await session.scalar(select(Source).where(Source.id == source_id)) is not None
    alice_link = select(UserSource).where(
        UserSource.user_id == alice.user.id, UserSource.source_id == source_id
    )
    assert await session.scalar(alice_link) is not None
    alice_history = select(func.count()).select_from(WatchHistory)
    alice_history = alice_history.where(WatchHistory.user_id == alice.user.id)
    assert await session.scalar(alice_history) == 2


async def test_rss_errors_map_to_http_statuses(
    client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    respx_mock.get("https://letterboxd.com/missing/rss/").mock(return_value=httpx.Response(404))
    user = await make_committed_user("rss_errors_user")
    await login(client, user.login_token)
    assert (await client.post("/api/sources", json={"input": "missing"})).status_code == 404
    assert (await client.post("/api/sources", json={"input": "bad user!"})).status_code == 400
    refresh = await client.post(f"/api/sources/{uuid4()}/refresh")
    assert refresh.status_code == 404


async def _seed_issue(
    session: AsyncSession,
    *,
    user: ProvisionedUser,
    candidates: list[dict[str, object]],
    raw_title: str = "Ambiguous Title",
    raw_year: int | None = 2020,
    item_kind: str = "rating",
    role: str = "self",
):
    source = await sources_repo.get_or_create_source(
        session,
        kind="lb_profile_csv",
        owner_user_id=user.user.id,
        external_key="profile",
        label="Letterboxd Export",
        url=None,
    )
    await sources_service.subscribe_user_to_source(
        session, user_id=user.user.id, source=source, role=role
    )
    issue, _ = await sources_repo.upsert_import_issue(
        session,
        user_id=user.user.id,
        source_id=source.id,
        item_kind=item_kind,
        raw_title=raw_title,
        raw_year=raw_year,
        letterboxd_url="https://boxd.it/x",
        rating=7.0,
        watched_at=datetime(2026, 10, 1, tzinfo=UTC),
        confidence=0.6,
        candidate_matches=candidates,
    )
    return source, issue


async def test_resolve_import_issue_writes_stub_item_and_history_without_tmdb(
    client: httpx.AsyncClient,
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("resolve_user")
    await login(client, user.login_token)
    candidate = {
        "media_type": "movie",
        "tmdb_id": 87101,
        "name": "Terminator Genisys",
        "year": 2015,
        "confidence": 0.6,
    }
    source, issue = await _seed_issue(session, user=user, candidates=[candidate])

    resp = await client.post(
        f"/api/import-issues/{issue.id}/resolve", json={"media_type": "movie", "tmdb_id": 87101}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["resolved_tmdb_id"] == 87101

    stub = await session.scalar(select(MediaItem).where(MediaItem.tmdb_id == 87101))
    assert stub is not None
    assert (stub.name, stub.year, stub.hydrated_at) == ("Terminator Genisys", 2015, None)
    item = await session.scalar(select(SourceItem).where(SourceItem.source_id == source.id))
    assert item is not None
    assert (item.tmdb_id, item.item_kind, item.rating) == (87101, "rating", 7.0)
    history = await session.scalar(select(WatchHistory).where(WatchHistory.user_id == user.user.id))
    assert history is not None
    assert (history.tmdb_id, history.rating, history.source_id) == (87101, 7.0, source.id)
    refreshed = await session.scalar(select(Source).where(Source.id == source.id))
    assert refreshed is not None
    assert refreshed.item_count == 1
    assert respx_mock.calls.call_count == 0

    # A resolved issue cannot be resolved again.
    again = await client.post(
        f"/api/import-issues/{issue.id}/resolve", json={"media_type": "movie", "tmdb_id": 603}
    )
    assert again.status_code == 409


async def test_resolve_uses_raw_title_when_the_choice_is_not_a_candidate(
    client: httpx.AsyncClient,
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("resolve_raw_user")
    await login(client, user.login_token)
    _, issue = await _seed_issue(
        session, user=user, candidates=[], raw_title="Unknown Short", raw_year=2021
    )

    resp = await client.post(
        f"/api/import-issues/{issue.id}/resolve", json={"media_type": "series", "tmdb_id": 555}
    )
    assert resp.status_code == 200
    stub = await session.scalar(
        select(MediaItem).where(MediaItem.media_type == "series", MediaItem.tmdb_id == 555)
    )
    assert stub is not None
    assert (stub.name, stub.year) == ("Unknown Short", 2021)
    assert respx_mock.calls.call_count == 0


async def test_resolve_rejects_out_of_range_ids_and_unknown_issues(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("resolve_invalid_user")
    await login(client, user.login_token)
    _, issue = await _seed_issue(session, user=user, candidates=[])
    url = f"/api/import-issues/{issue.id}/resolve"
    for body in (
        {"media_type": "movie", "tmdb_id": 0},
        {"media_type": "movie", "tmdb_id": 2**31},
        {"media_type": "tv", "tmdb_id": 1},
    ):
        assert (await client.post(url, json=body)).status_code == 422
    missing = await client.post(
        f"/api/import-issues/{uuid4()}/resolve", json={"media_type": "movie", "tmdb_id": 1}
    )
    assert missing.status_code == 404


async def test_other_users_cannot_resolve_or_dismiss_an_issue(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    owner = await make_committed_user("issue_owner")
    other = await make_committed_user("issue_other")
    _, issue = await _seed_issue(session, user=owner, candidates=[])
    await login(client, other.login_token)
    resolve = await client.post(
        f"/api/import-issues/{issue.id}/resolve", json={"media_type": "movie", "tmdb_id": 1}
    )
    assert resolve.status_code == 404
    assert (await client.post(f"/api/import-issues/{issue.id}/dismiss")).status_code == 404
    assert (await client.get("/api/import-issues")).json()["issues"] == []


async def test_dismiss_import_issue(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    user = await make_committed_user("dismiss_user")
    await login(client, user.login_token)
    _, issue = await _seed_issue(session, user=user, candidates=[])
    pending = await client.get("/api/import-issues", params={"status": "pending"})
    assert pending.json()["pending_count"] == 1
    dismiss = await client.post(f"/api/import-issues/{issue.id}/dismiss")
    assert dismiss.status_code == 200
    assert dismiss.json()["status"] == "dismissed"
    stored = await session.scalar(select(ImportIssue).where(ImportIssue.id == issue.id))
    assert stored is not None
    assert stored.status == "dismissed"
    pending = await client.get("/api/import-issues", params={"status": "pending"})
    assert pending.json() == {"issues": [], "pending_count": 0}
