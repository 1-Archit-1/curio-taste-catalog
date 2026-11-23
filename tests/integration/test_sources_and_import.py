"""Integration tests for Issue #5: typed sources, Letterboxd RSS, ZIP import, and issues."""

import io
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import TmdbClient
from curio.crypto import Keypair
from curio.db.models.catalogs import MediaItem, WatchHistory
from curio.db.models.sources import ImportIssue, Source, SourceItem, UserSource
from curio.services import keys as keys_service
from curio.services import sources as sources_service
from curio.services.users import ProvisionedUser
from curio.settings import Settings

LB_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "letterboxd"
TMDB_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tmdb"


def _build_fixture_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel in (
            "profile.csv",
            "watched.csv",
            "ratings.csv",
            "diary.csv",
            "watchlist.csv",
            "lists/sci-fi-favorites.csv",
        ):
            zf.write(LB_FIXTURES / rel, arcname=rel)
    return buf.getvalue()


async def _login_with_token(c: httpx.AsyncClient, token: str) -> None:
    resp = await c.post("/api/auth/login", json={"token": token})
    assert resp.status_code == 200


def _mock_tmdb_for_zip(respx_mock: respx.MockRouter) -> None:
    movie_603 = (TMDB_FIXTURES / "movie_603.json").read_text(encoding="utf-8")
    movie_87101 = (TMDB_FIXTURES / "movie_87101.json").read_text(encoding="utf-8")

    def search_handler(request: httpx.Request) -> httpx.Response:
        query = (request.url.params.get("query") or "").lower()
        if "matrix" in query:
            return httpx.Response(
                200,
                json={
                    "results": [{"id": 603, "title": "The Matrix", "release_date": "1999-03-31"}]
                },
            )
        if "terminator" in query:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 87101, "title": "Terminator Genisys", "release_date": "2015-07-01"}
                    ]
                },
            )
        if "ambiguous" in query:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 603, "title": "Something Different", "release_date": "2020-01-01"}
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    respx_mock.get("https://api.themoviedb.org/3/search/movie").mock(side_effect=search_handler)
    respx_mock.get("https://api.themoviedb.org/3/movie/603").mock(
        return_value=httpx.Response(200, text=movie_603)
    )
    respx_mock.get("https://api.themoviedb.org/3/movie/87101").mock(
        return_value=httpx.Response(200, text=movie_87101)
    )


async def test_sources_reject_unauthenticated_requests(client: httpx.AsyncClient):
    assert (await client.get("/api/sources")).status_code == 401
    assert (await client.post("/api/sources/preview", json={"input": "alice"})).status_code == 401
    assert (await client.get("/api/import-issues")).status_code == 401


async def test_rss_preview_add_shared_refresh_role_and_remove(
    client: httpx.AsyncClient,
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    rss_xml = (LB_FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")
    rss_route = respx_mock.get("https://letterboxd.com/alice/rss/").mock(
        return_value=httpx.Response(200, text=rss_xml)
    )

    alice = await make_committed_user("rss_alice")
    bob = await make_committed_user("rss_bob")

    await _login_with_token(client, alice.login_token)

    # Preview RSS feed
    prev_resp = await client.post(
        "/api/sources/preview", json={"input": "https://letterboxd.com/alice/"}
    )
    assert prev_resp.status_code == 200
    prev_data = prev_resp.json()
    assert prev_data["username"] == "alice"
    assert prev_data["entry_count"] == 2
    assert prev_data["recent_titles"][0]["title"] == "The Matrix"

    # Alice adds her own diary RSS with role='self'
    add_resp = await client.post("/api/sources", json={"input": "alice", "role": "self"})
    assert add_resp.status_code == 201
    alice_source = add_resp.json()
    assert alice_source["kind"] == "lb_diary_rss"
    assert alice_source["role"] == "self"
    assert alice_source["item_count"] == 2
    source_id = UUID(alice_source["id"])

    # Alice's watch_history is synced (5.0 stars -> 10.0 on Curio scale)
    wh_rows = list(
        await session.scalars(select(WatchHistory).where(WatchHistory.user_id == alice.user.id))
    )
    assert len(wh_rows) == 2
    by_id = {(r.media_type, r.tmdb_id): r for r in wh_rows}
    assert by_id[("movie", 603)].rating == 10.0
    assert by_id[("movie", 603)].source == "letterboxd"
    assert by_id[("series", 1396)].rating == 9.0

    # Bob subscribes to the same shared source with role='taste'
    await _login_with_token(client, bob.login_token)
    bob_add = await client.post(
        "/api/sources", json={"input": "https://letterboxd.com/alice/", "role": "taste"}
    )
    assert bob_add.status_code == 201
    assert bob_add.json()["id"] == alice_source["id"]

    # Only one shared Source row exists in DB
    source_count = await session.scalar(
        select(func.count()).select_from(Source).where(Source.id == source_id)
    )
    assert source_count == 1

    # Bob has no watch_history yet because his role is 'taste'
    bob_wh_count = await session.scalar(
        select(func.count()).select_from(WatchHistory).where(WatchHistory.user_id == bob.user.id)
    )
    assert bob_wh_count == 0

    # Refreshing once fetches the shared RSS feed once
    calls_before = rss_route.call_count
    refresh_resp = await client.post(f"/api/sources/{source_id}/refresh")
    assert refresh_resp.status_code == 200
    assert rss_route.call_count == calls_before + 1

    # Changing Bob's role to 'self' syncs his watch_history
    patch_resp = await client.patch(f"/api/sources/{source_id}", json={"role": "self"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["role"] == "self"
    bob_wh_count = await session.scalar(
        select(func.count()).select_from(WatchHistory).where(WatchHistory.user_id == bob.user.id)
    )
    assert bob_wh_count == 2

    # Bob removes his subscription; shared Source & Alice's subscription remain
    del_resp = await client.delete(f"/api/sources/{source_id}")
    assert del_resp.status_code == 200
    assert (await session.scalar(select(Source).where(Source.id == source_id))) is not None
    assert (
        await session.scalar(
            select(UserSource).where(
                UserSource.user_id == alice.user.id, UserSource.source_id == source_id
            )
        )
    ) is not None


async def test_zip_import_service_with_encrypted_key_and_audit(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    respx_mock: respx.MockRouter,
    keypair: Keypair,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    """Verify ZIP import & issue resolution decrypt the user's TMDB key via ADR 0002."""
    provisioned = await make_committed_user("zip_svc_alice")
    respx_mock.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=httpx.Response(200, json={"images": {}})
    )
    await keys_service.set_user_api_key(
        session,
        user_id=provisioned.user.id,
        key_type=keys_service.KeyType.TMDB,
        api_key="fake-tmdb-key-1234",
        public_key=keypair.public_key,
    )
    await session.flush()

    _mock_tmdb_for_zip(respx_mock)
    worker_settings = Settings(
        _env_file=None,
        database_url=migrated_database_url,
        key_public_key=keypair.public_key,
        key_private_key=keypair.private_key,
    )

    result = await sources_service.import_zip_sources(
        session,
        user_id=provisioned.user.id,
        zip_bytes=_build_fixture_zip(),
        role="self",
        settings=worker_settings,
        audit_session_factory=audit_session_factory,
    )
    assert len(result.sources) == 2
    assert result.matched_count == 5
    assert result.issue_count == 1

    issues, pending = await sources_service.list_import_issues(
        session, user_id=provisioned.user.id, status="pending"
    )
    assert pending == 1

    resolved = await sources_service.resolve_import_issue(
        session,
        user_id=provisioned.user.id,
        issue_id=issues[0].id,
        media_type="movie",
        tmdb_id=87101,
        settings=worker_settings,
        audit_session_factory=audit_session_factory,
    )
    assert resolved.status == "resolved"
    assert resolved.resolved_tmdb_id == 87101


async def test_zip_import_endpoints_idempotent_and_issue_actions(
    client: httpx.AsyncClient,
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    provisioned = await make_committed_user("zip_http_alice")
    await _login_with_token(client, provisioned.login_token)

    zip_bytes = _build_fixture_zip()
    # Without TMDB key / private key, ZIP import returns 400
    no_key_resp = await client.post(
        "/api/sources/import-zip",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
        data={"role": "self"},
    )
    assert no_key_resp.status_code == 400

    _mock_tmdb_for_zip(respx_mock)

    async def fake_tmdb_client_for_user(*args, **kwargs) -> TmdbClient:
        return TmdbClient("fake-tmdb-key-1234", RateLimiter(100.0))

    monkeypatch.setattr(
        sources_service.media_service, "tmdb_client_for_user", fake_tmdb_client_for_user
    )

    # First ZIP import
    resp = await client.post(
        "/api/sources/import-zip",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
        data={"role": "self"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["sources"]) == 2
    assert body["matched_count"] == 5
    assert body["issue_count"] == 1

    # Verify watch_history has The Matrix but NOT Terminator Genisys (watchlist)
    wh_rows = list(
        await session.scalars(
            select(WatchHistory).where(WatchHistory.user_id == provisioned.user.id)
        )
    )
    assert [(r.media_type, r.tmdb_id) for r in wh_rows] == [("movie", 603)]
    assert wh_rows[0].rating == 10.0

    # Verify watchlist item is stored as SourceItem(item_kind='watchlist')
    wl_item = await session.scalar(
        select(SourceItem).where(SourceItem.tmdb_id == 87101, SourceItem.item_kind == "watchlist")
    )
    assert wl_item is not None

    # Verify MediaItem was hydrated
    matrix = await session.scalar(
        select(MediaItem).where(MediaItem.media_type == "movie", MediaItem.tmdb_id == 603)
    )
    assert matrix is not None
    assert matrix.hydrated_at is not None

    # Re-import is idempotent
    resp2 = await client.post(
        "/api/sources/import-zip",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
        data={"role": "self"},
    )
    assert resp2.status_code == 200
    issues_resp = await client.get("/api/import-issues", params={"status": "pending"})
    assert issues_resp.status_code == 200
    issues_data = issues_resp.json()
    assert issues_data["pending_count"] == 1
    issue_id = issues_data["issues"][0]["id"]
    assert issues_data["issues"][0]["raw_title"] == "Ambiguous Title"
    assert len(issues_data["issues"][0]["candidate_matches"]) == 1

    # Resolve the import issue to TMDB 87101
    resolve_resp = await client.post(
        f"/api/import-issues/{issue_id}/resolve",
        json={"media_type": "movie", "tmdb_id": 87101},
    )
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["status"] == "resolved"
    assert resolve_resp.json()["resolved_tmdb_id"] == 87101

    wh_87101 = await session.scalar(
        select(WatchHistory).where(
            WatchHistory.user_id == provisioned.user.id,
            WatchHistory.media_type == "movie",
            WatchHistory.tmdb_id == 87101,
        )
    )
    assert wh_87101 is not None

    # Re-importing after resolution does not recreate the pending issue
    resp3 = await client.post(
        "/api/sources/import-zip",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
        data={"role": "self"},
    )
    assert resp3.status_code == 200
    assert resp3.json()["issue_count"] == 0
    assert resp3.json()["skipped_count"] == 1


async def test_dismiss_import_issue_prevents_recreation(
    client: httpx.AsyncClient,
    session: AsyncSession,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    make_committed_user: Callable[[str], Awaitable[ProvisionedUser]],
):
    provisioned = await make_committed_user("dismiss_alice")
    await _login_with_token(client, provisioned.login_token)

    _mock_tmdb_for_zip(respx_mock)

    async def fake_tmdb_client_for_user(*args, **kwargs) -> TmdbClient:
        return TmdbClient("fake-tmdb-key-1234", RateLimiter(100.0))

    monkeypatch.setattr(
        sources_service.media_service, "tmdb_client_for_user", fake_tmdb_client_for_user
    )

    zip_bytes = _build_fixture_zip()
    resp = await client.post(
        "/api/sources/import-zip",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
        data={"role": "taste"},
    )
    assert resp.status_code == 200

    issue = list(
        await session.scalars(select(ImportIssue).where(ImportIssue.user_id == provisioned.user.id))
    )[0]

    dismiss_resp = await client.post(f"/api/import-issues/{issue.id}/dismiss")
    assert dismiss_resp.status_code == 200
    assert dismiss_resp.json()["status"] == "dismissed"

    resp2 = await client.post(
        "/api/sources/import-zip",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
        data={"role": "taste"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["issue_count"] == 0
    assert resp2.json()["skipped_count"] == 1
    assert await sources_service.list_import_issues(
        session, user_id=provisioned.user.id, status="pending"
    ) == ([], 0)
