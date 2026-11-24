"""The `simkl_sync` job: two-phase incremental sync, token refresh, errors, and history."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.crypto import KeyDecryptor, Keypair
from curio.db.models.catalogs import MediaItem, WatchHistory
from curio.db.models.jobs import Job
from curio.db.models.keys import KeyDecryptAudit
from curio.db.models.simkl import SimklConnection
from curio.db.models.sources import Source, SourceItem
from curio.repositories import catalogs as catalogs_repo
from curio.repositories import sources as sources_repo
from curio.services import jobs as jobs_service
from curio.services import sources as sources_service
from curio.services.users import ProvisionedUser
from tests.integration.lb_support import login
from tests.integration.simkl_support import (
    ACCESS,
    LATER,
    NEW_ACCESS,
    REFRESH,
    STAMP,
    SimklMock,
    activities,
    all_requests_identified,
    assert_secrets_absent,
    configure_api,
    delete_test_titles,
    entry,
    load,
    make_due,
    run_worker,
    seed_connection,
    worker_settings,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

MakeUser = Callable[[str], Awaitable[ProvisionedUser]]
SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
async def _clean_titles(audit_session_factory: SessionFactory):
    yield
    await delete_test_titles(audit_session_factory)


@pytest.fixture
def simkl(respx_mock: respx.MockRouter, committed_app: FastAPI) -> SimklMock:
    configure_api(committed_app)
    return SimklMock(respx_mock)


class Env:
    """One logged-in user with a Simkl connection, and ways to run and inspect syncs."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        factory: SessionFactory,
        database_url: str,
        keypair: Keypair,
        user: ProvisionedUser,
        source_id: UUID,
    ) -> None:
        self.client = client
        self.factory = factory
        self.database_url = database_url
        self.keypair = keypair
        self.user = user
        self.uid = user.user.id
        self.source_id = source_id

    async def sync(self) -> dict[str, Any]:
        """Queue a sync through the API, run it in the worker, and return the job's status."""
        resp = await self.client.post("/api/simkl/sync")
        assert resp.status_code == 202, resp.text
        assert await run_worker(self.factory, self.database_url, self.keypair)
        return (await self.client.get(f"/api/jobs/{resp.json()['id']}")).json()

    async def connection(self) -> SimklConnection:
        async with self.factory() as s:
            row = await s.get(SimklConnection, self.uid)
        assert row is not None
        return row

    async def items(self) -> dict[tuple[str, int], tuple[str, float | None]]:
        async with self.factory() as s:
            rows = await s.scalars(select(SourceItem).where(SourceItem.source_id == self.source_id))
            return {(r.media_type, r.tmdb_id): (r.item_kind, r.rating) for r in rows}

    async def history(self) -> dict[tuple[str, int], WatchHistory]:
        async with self.factory() as s:
            rows = await s.scalars(select(WatchHistory).where(WatchHistory.user_id == self.uid))
            return {(r.media_type, r.tmdb_id): r for r in rows}

    async def source(self) -> Source:
        async with self.factory() as s:
            row = await s.get(Source, self.source_id)
        assert row is not None
        return row


@pytest.fixture
async def env(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
) -> Env:
    user = await make_committed_user("simkl_sync_user")
    await login(committed_client, user.login_token)
    source_id = await seed_connection(audit_session_factory, keypair, user.user.id)
    return Env(
        committed_client,
        audit_session_factory,
        migrated_database_url,
        keypair,
        user,
        source_id,
    )


async def _run_first_sync(env: Env, simkl: SimklMock) -> dict[str, Any]:
    simkl.set_activities(load("activities.json"))
    status = await env.sync()
    assert status["job"]["status"] == "succeeded", status
    return status


# --- First run and no-change runs -------------------------------------------------------------


async def test_first_sync_pulls_every_type_in_full_and_writes_status_ratings_and_history(
    env: Env, simkl: SimklMock
):
    status = await _run_first_sync(env, simkl)

    # Full pull: each type once, no `date_from`, and no separate ratings call.
    assert simkl.library_calls() == [
        "/sync/all-items/shows",
        "/sync/all-items/movies",
        "/sync/all-items/anime",
    ]
    assert not any(route.called for route in simkl.ratings.values())
    assert all_requests_identified(simkl)
    # Activities were read before the library, and that response is the snapshot.
    paths = [c.request.url.path for c in simkl.router.calls]
    assert paths[0] == "/sync/activities"

    items = await env.items()
    assert items == {
        ("series", 1396): ("watched", 10.0),  # completed -> watched; rating as Simkl sent it
        ("series", 1438): ("dropped", 4.0),
        ("series", 70523): ("watching", None),
        ("movie", 603): ("watched", 9.0),
        ("movie", 680): ("dropped", 5.0),
        ("movie", 238): ("watched", None),
        ("series", 30991): ("watched", 10.0),  # anime
        ("movie", 129): ("watched", 9.0),  # anime movie
        ("series", 1429): ("watching", 8.0),  # two entries on one TMDB id: watching wins
    }
    # plantowatch (1981, 438631) and hold (87108) are not history.
    assert not {1981, 87108, 438631} & {tmdb for _, tmdb in items}

    history = await env.history()
    assert {k: (h.status, h.rating, h.source) for k, h in history.items()} == {
        ("series", 1396): ("watched", 10.0, "simkl"),
        ("series", 1438): ("dropped", 4.0, "simkl"),
        ("series", 70523): ("watching", None, "simkl"),
        ("movie", 603): ("watched", 9.0, "simkl"),
        ("movie", 680): ("dropped", 5.0, "simkl"),
        ("movie", 238): ("watched", None, "simkl"),
        ("series", 30991): ("watched", 10.0, "simkl"),
        ("movie", 129): ("watched", 9.0, "simkl"),
        ("series", 1429): ("watching", 8.0, "simkl"),
    }
    assert all(h.source_id == env.source_id for h in history.values())

    # A dropped series shows up as anti-taste.
    async with env.factory() as s:
        anti = await catalogs_repo.list_anti_taste(s, user_id=env.uid)
    series = [(r.tmdb_id, r.reason, r.rating) for r in anti if r.media_type == "series"]
    assert (1438, "dropped_series", 4.0) in series
    assert {tmdb for tmdb, _, _ in series} == {1438}  # nothing else on the list

    connection = await env.connection()
    assert connection.activities_snapshot == load("activities.json")
    assert connection.last_synced_at is not None and connection.last_error is None
    source = await env.source()
    assert (source.item_count, source.last_error) == (9, None)
    assert source.last_refreshed_at is not None

    # Stubs for new titles, no TMDB calls (respx would have failed on any other host).
    async with env.factory() as s:
        stub = await s.get(MediaItem, ("series", 1438))
    assert stub is not None and stub.name == "The Wire" and stub.hydrated_at is None
    done = [e for e in status["events"] if e["step"] == "sync"][0]
    # The show and the anime without a TMDB id are skipped and counted.
    assert done["payload"] == {
        "synced": 9,
        "removed": 0,
        "skipped_without_tmdb": 2,
        "unchanged": False,
    }
    assert [e["step"] for e in status["events"]][-1] == "completed"


async def test_a_sync_with_unchanged_activities_fetches_nothing_new(env: Env, simkl: SimklMock):
    await _run_first_sync(env, simkl)
    calls_before = len(simkl.router.calls)
    status = await env.sync()
    assert status["job"]["status"] == "succeeded"
    new_paths = [c.request.url.path for c in list(simkl.router.calls)[calls_before:]]
    assert new_paths == ["/sync/activities"]
    done = [e for e in status["events"] if e["step"] == "sync"][0]
    assert done["message"] == "Nothing changed on Simkl."
    assert done["payload"]["unchanged"] is True
    assert len(await env.items()) == 9


async def test_activities_that_moved_without_a_list_change_only_update_the_snapshot(
    env: Env, simkl: SimklMock
):
    await _run_first_sync(env, simkl)
    calls_before = len(simkl.router.calls)
    moved = activities(all_=LATER)  # e.g. settings or playback changed
    simkl.set_activities(moved)
    assert (await env.sync())["job"]["status"] == "succeeded"
    new_paths = [c.request.url.path for c in list(simkl.router.calls)[calls_before:]]
    assert new_paths == ["/sync/activities"]
    assert (await env.connection()).activities_snapshot == moved


# --- Incremental syncs ------------------------------------------------------------------------


async def test_incremental_sync_uses_date_from_and_only_fetches_the_types_that_moved(
    env: Env, simkl: SimklMock
):
    await _run_first_sync(env, simkl)
    simkl.router.calls.clear()
    # tv_shows: Dark (watching) is finished and The Wire is moved to plan to watch. Charmed
    # (plan to watch) is started. anime: a new title is added.
    simkl.delta_by_type["shows"] = {
        "shows": [
            entry("show", "Dark", 70523, "completed", rating=9),
            entry("show", "The Wire", 1438, "plantowatch"),
            entry("show", "Charmed", 1981, "watching"),
        ]
    }
    simkl.delta_by_type["anime"] = {
        "anime": [entry("show", "Naruto", 900001, "completed", rating=7, anime_type="tv")]
    }
    moved = activities(
        all_=LATER,
        shows={"completed": LATER, "watching": LATER, "plantowatch": LATER},
        anime={"completed": LATER},
    )
    simkl.set_activities(moved)
    simkl.all_items_any.mock(
        return_value=httpx.Response(
            200, json={**simkl.delta_by_type["shows"], **simkl.delta_by_type["anime"]}
        )
    )
    status = await env.sync()
    assert status["job"]["status"] == "succeeded"

    # Two types moved: one combined delta call, `date_from` = the saved snapshot's `all`.
    assert simkl.library_calls() == [f"/sync/all-items?{{'date_from': '{STAMP}'}}"]
    assert not any(route.called for route in simkl.ratings.values())
    items = await env.items()
    assert items[("series", 70523)] == ("watched", 9.0)  # one row per title, not watching + watched
    assert ("series", 1438) not in items  # moved to plantowatch: gone
    assert items[("series", 1981)] == ("watching", None)
    assert items[("series", 900001)] == ("watched", 7.0)
    assert items[("series", 1396)] == ("watched", 10.0)  # untouched
    assert len(items) == 10  # 9, less The Wire, plus Charmed and Naruto
    history = await env.history()
    assert ("series", 1438) not in history  # no longer dropped, so no longer anti-taste
    assert history[("series", 70523)].status == "watched"
    assert history[("series", 1981)].status == "watching"
    assert (await env.connection()).activities_snapshot == moved


@pytest.mark.parametrize(
    ("bucket", "buckets", "type_path", "body_key"),
    [
        ("tv_shows", {"completed": LATER}, "shows", "shows"),
        ("anime", {"watching": LATER}, "anime", "anime"),
        ("movies", {"dropped": LATER}, "movies", "movies"),
    ],
)
async def test_one_moved_type_is_fetched_with_its_own_path_and_activity_name(
    env: Env,
    simkl: SimklMock,
    bucket: str,
    buckets: dict[str, str],
    type_path: str,
    body_key: str,
):
    await _run_first_sync(env, simkl)
    simkl.router.calls.clear()
    key = {"tv_shows": "shows", "anime": "anime", "movies": "movies"}[bucket]
    simkl.set_activities(activities(all_=LATER, **{key: buckets}))
    assert (await env.sync())["job"]["status"] == "succeeded"
    assert simkl.library_calls() == [f"/sync/all-items/{type_path}?{{'date_from': '{STAMP}'}}"]


async def test_a_title_moved_to_a_status_that_isnt_history_disappears(env: Env, simkl: SimklMock):
    await _run_first_sync(env, simkl)
    simkl.delta_by_type["movies"] = {"movies": [entry("movie", "The Matrix", 603, "plantowatch")]}
    simkl.delta_by_type["shows"] = {"shows": [entry("show", "Dark", 70523, "hold")]}
    simkl.set_activities(
        activities(all_=LATER, movies={"plantowatch": LATER}, shows={"hold": LATER})
    )
    simkl.all_items_any.mock(
        return_value=httpx.Response(
            200, json={**simkl.delta_by_type["movies"], **simkl.delta_by_type["shows"]}
        )
    )
    assert (await env.sync())["job"]["status"] == "succeeded"
    items, history = await env.items(), await env.history()
    for title in (("movie", 603), ("series", 70523)):
        assert title not in items and title not in history


async def test_a_rating_change_comes_from_the_ratings_delta_not_the_library(
    env: Env, simkl: SimklMock
):
    await _run_first_sync(env, simkl)
    simkl.router.calls.clear()
    simkl.ratings["movies"].mock(return_value=httpx.Response(200, json=load("ratings_movies.json")))
    simkl.ratings["shows"].mock(return_value=httpx.Response(200, json=load("ratings_shows.json")))
    simkl.set_activities(
        activities(all_=LATER, movies={"rated_at": LATER}, shows={"rated_at": LATER})
    )
    assert (await env.sync())["job"]["status"] == "succeeded"

    # Ratings only, always with `date_from`, never a rating segment, no library request.
    ratings_calls = [c.request for c in simkl.router.calls if "/sync/ratings" in c.request.url.path]
    assert sorted(r.url.path for r in ratings_calls) == [
        "/sync/ratings/movies",
        "/sync/ratings/shows",
    ]
    assert all(r.url.params["date_from"] == STAMP for r in ratings_calls)
    assert simkl.library_calls() == []
    items = await env.items()
    assert items[("movie", 603)] == ("watched", 10.0)  # 9 -> 10
    assert items[("series", 1396)] == ("watched", 9.0)  # 10 -> 9
    assert items[("series", 1438)] == ("dropped", None)  # `user_rating: null` clears it
    history = await env.history()
    assert (
        history[("series", 1438)].rating is None and history[("series", 1438)].status == "dropped"
    )
    assert history[("movie", 603)].rating == 10.0


async def test_removed_items_are_found_by_diffing_the_id_lists(env: Env, simkl: SimklMock):
    await _run_first_sync(env, simkl)
    simkl.router.calls.clear()
    # The user deleted Dark (a show) and Spirited Away (an anime movie) from Simkl.
    simkl.ids["shows"] = {
        "shows": [
            row
            for row in load("ids_only_shows.json")["shows"]
            if row["show"]["ids"].get("tmdb") != "70523"
        ]
    }
    simkl.ids["anime"] = {
        "anime": [
            row
            for row in load("ids_only_anime.json")["anime"]
            if row["show"]["ids"].get("tmdb") != "129"
        ]
    }
    simkl.set_activities(activities(all_=LATER, shows={"removed_from_list": LATER}))
    status = await env.sync()
    assert status["job"]["status"] == "succeeded"

    # Every type's ids are fetched, so a title only counts as gone when no list has it.
    assert simkl.library_calls() == [
        "/sync/all-items/shows?{'extended': 'ids_only'}",
        "/sync/all-items/movies?{'extended': 'ids_only'}",
        "/sync/all-items/anime?{'extended': 'ids_only'}",
    ]
    items, history = await env.items(), await env.history()
    for title in (("series", 70523), ("movie", 129)):
        assert title not in items and title not in history
    assert len(items) == 7
    done = [e for e in status["events"] if e["step"] == "sync"][0]
    assert done["payload"]["removed"] == 2


async def test_removal_check_keeps_titles_an_anime_id_could_still_cover(env: Env, simkl: SimklMock):
    await _run_first_sync(env, simkl)
    simkl.set_activities(activities(all_=LATER, movies={"removed_from_list": LATER}))
    assert (await env.sync())["job"]["status"] == "succeeded"
    assert len(await env.items()) == 9  # nothing was removed on Simkl


# --- Failure handling -------------------------------------------------------------------------


async def test_the_snapshot_is_saved_with_the_items_and_not_at_all_when_the_sync_fails(
    env: Env, simkl: SimklMock, monkeypatch: pytest.MonkeyPatch
):
    await _run_first_sync(env, simkl)
    before = await env.connection()
    simkl.delta_by_type["movies"] = {"movies": [entry("movie", "New Movie", 900002, "completed")]}
    moved = activities(all_=LATER, movies={"completed": LATER})
    simkl.set_activities(moved)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database trouble with a secret detail")

    monkeypatch.setattr(sources_service, "resync_watch_history", boom)
    status = await env.sync()
    assert status["job"]["status"] == "queued"  # a failed attempt is retried
    assert status["job"]["last_error"] == "Unexpected error (RuntimeError). See worker logs."

    after = await env.connection()
    assert after.activities_snapshot == before.activities_snapshot  # not the new one
    assert ("movie", 900002) not in await env.items()  # rolled back with the failed sync
    # The error survives the rollback, and a detail of the exception is never shown.
    assert after.last_error == "Unexpected error while syncing Simkl. See worker logs."
    assert (await env.source()).last_error == after.last_error
    assert after.needs_reconnect is False

    monkeypatch.undo()
    async with env.factory() as s:
        job = await s.scalar(select(Job).where(Job.user_id == env.uid, Job.status == "queued"))
    assert job is not None
    await make_due(env.factory, job.id)
    assert await run_worker(env.factory, env.database_url, env.keypair)
    recovered = await env.connection()
    assert recovered.activities_snapshot == moved
    assert recovered.last_error is None and (await env.source()).last_error is None
    assert ("movie", 900002) in await env.items()


async def test_a_simkl_outage_is_recorded_and_retried(env: Env, simkl: SimklMock):
    simkl.activities.mock(return_value=httpx.Response(503))
    status = await env.sync()
    assert status["job"]["status"] == "queued"
    assert status["job"]["last_error"] == "Simkl is unavailable (HTTP 503)."
    connection = await env.connection()
    assert connection.last_error == "Simkl is unavailable (HTTP 503)."
    assert connection.needs_reconnect is False and connection.activities_snapshot is None
    assert (await env.source()).last_error == "Simkl is unavailable (HTTP 503)."


async def test_the_daily_quota_fails_the_job_without_retrying(env: Env, simkl: SimklMock):
    simkl.activities.mock(
        return_value=httpx.Response(
            429, json={"error": "user_limit_exceeded"}, headers={"Retry-After": "3600"}
        )
    )
    status = await env.sync()
    assert status["job"]["status"] == "failed"
    assert "daily request limit" in status["job"]["last_error"]
    assert "daily request limit" in (await env.connection()).last_error


async def test_a_server_without_simkl_credentials_fails_the_job_clearly(env: Env, simkl: SimklMock):
    resp = await env.client.post("/api/simkl/sync")
    assert resp.status_code == 202
    assert await run_worker(
        env.factory,
        env.database_url,
        env.keypair,
        settings=worker_settings(env.database_url, env.keypair, simkl_client_id=None),
    )
    status = (await env.client.get(f"/api/jobs/{resp.json()['id']}")).json()
    assert status["job"]["status"] == "failed"
    assert status["job"]["last_error"] == "Simkl isn't set up on this server."
    assert len(simkl.router.calls) == 0


# --- Tokens -----------------------------------------------------------------------------------


async def test_a_token_close_to_expiry_is_refreshed_before_the_sync(env: Env, simkl: SimklMock):
    async with env.factory() as s:
        await s.execute(
            text("UPDATE simkl_connections SET access_expires_at = now() + interval '23 hours'")
        )
        await s.commit()
    await _run_first_sync(env, simkl)

    refresh = simkl.token.calls.last.request
    assert simkl.token.call_count == 1
    assert refresh.content.decode() == f"grant_type=refresh_token&refresh_token={REFRESH}"
    assert refresh.headers["authorization"].startswith("Basic ")
    # The refreshed token is used for every call after it, and the order is refresh first.
    names = [c.request.url.path for c in simkl.router.calls]
    assert names[0] == "/oauth2/token"
    simkl_calls = [c.request for c in simkl.router.calls if c.request.url.path != "/oauth2/token"]
    assert {r.headers["authorization"] for r in simkl_calls} == {f"Bearer {NEW_ACCESS}"}

    connection = await env.connection()
    decryptor = KeyDecryptor(env.keypair.private_key)
    assert decryptor.decrypt(connection.access_token_ciphertext) == NEW_ACCESS
    assert decryptor.decrypt(connection.refresh_token_ciphertext) == REFRESH
    assert timedelta(days=6) < connection.access_expires_at - datetime.now(UTC) <= timedelta(days=7)
    await assert_secrets_absent(env.factory, ACCESS, NEW_ACCESS, REFRESH)
    # Every decryption is audited, with the job it served.
    async with env.factory() as s:
        audit = list(
            await s.scalars(select(KeyDecryptAudit).where(KeyDecryptAudit.user_id == env.uid))
        )
    assert audit and {a.key_type for a in audit} == {"simkl"}
    assert {a.reason for a in audit} == {"simkl sync"}
    assert all(a.job_id for a in audit)


async def test_a_fresh_token_is_not_refreshed(env: Env, simkl: SimklMock):
    await _run_first_sync(env, simkl)
    assert simkl.token.call_count == 0
    simkl_calls = [c.request for c in simkl.router.calls]
    assert {r.headers["authorization"] for r in simkl_calls} == {f"Bearer {ACCESS}"}


async def test_an_expired_grant_pauses_the_job_and_asks_for_a_reconnect(env: Env, simkl: SimklMock):
    async with env.factory() as s:
        await s.execute(
            text("UPDATE simkl_connections SET access_expires_at = now() + interval '1 hour'")
        )
        await s.commit()
    simkl.token.mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Refresh token expired"}
        )
    )
    status = await env.sync()

    job = status["job"]
    assert job["status"] == "paused" and job["paused_key_type"] == "simkl"
    assert job["paused_reason"] == "Simkl no longer accepts this connection. Reconnect Simkl."
    assert simkl.token.call_count == 1  # no retry loop on a dead grant
    connection = await env.connection()
    assert connection.needs_reconnect is True
    assert connection.last_error == "Simkl no longer accepts this connection. Reconnect Simkl."
    assert (await env.source()).last_error == connection.last_error
    status_body = (await env.client.get("/api/simkl/connection")).json()
    assert status_body["needs_reconnect"] is True
    # The user's other jobs and settings keep working; the pause is Simkl's alone.
    assert (await env.client.get("/api/settings")).json()["jobs_paused_reason"] is None
    # Syncing again is refused until they reconnect.
    again = await env.client.post("/api/simkl/sync")
    assert again.status_code == 409
    assert "Reconnect Simkl" in again.json()["detail"] or "reconnected" in again.json()["detail"]


async def test_a_rejected_access_token_is_refreshed_once_and_the_sync_retried(
    env: Env, simkl: SimklMock
):
    responses = iter(
        [
            httpx.Response(401, json={"error": "invalid_token", "code": 401}),
            httpx.Response(200, json=load("activities.json")),
        ]
    )
    simkl.activities.mock(side_effect=lambda request: next(responses))
    status = await env.sync()
    assert status["job"]["status"] == "succeeded"
    assert simkl.token.call_count == 1
    auth = [
        c.request.headers["authorization"]
        for c in simkl.router.calls
        if "activities" in str(c.request.url)
    ]
    assert auth == [f"Bearer {ACCESS}", f"Bearer {NEW_ACCESS}"]
    assert (await env.connection()).needs_reconnect is False


async def test_a_token_that_is_rejected_even_after_a_refresh_pauses_for_reconnect(
    env: Env, simkl: SimklMock
):
    simkl.activities.mock(return_value=httpx.Response(401, json={"error": "invalid_token"}))
    status = await env.sync()
    assert status["job"]["status"] == "paused"
    assert status["job"]["paused_key_type"] == "simkl"
    assert simkl.token.call_count == 1  # refreshed once, not in a loop
    assert (await env.connection()).needs_reconnect is True


async def test_a_bad_client_secret_fails_the_job_but_keeps_the_connection_usable(
    env: Env, simkl: SimklMock
):
    async with env.factory() as s:
        await s.execute(
            text("UPDATE simkl_connections SET access_expires_at = now() + interval '1 hour'")
        )
        await s.commit()
    simkl.token.mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    status = await env.sync()
    assert status["job"]["status"] == "failed"
    assert "SIMKL_CLIENT_SECRET" in status["job"]["last_error"]
    # The user did nothing wrong, so they aren't asked to reconnect.
    assert (await env.connection()).needs_reconnect is False


# --- Watch history provenance (ADR 0009) ------------------------------------------------------


async def test_simkl_never_overwrites_manual_or_letterboxd_history(env: Env, simkl: SimklMock):
    async with env.factory() as s:
        # A manual rating for The Matrix, and a Letterboxd export that has Breaking Bad.
        await sources_repo.ensure_media_item_stubs(
            s,
            [
                {"media_type": "movie", "tmdb_id": 603, "name": "The Matrix", "year": 1999},
                {"media_type": "series", "tmdb_id": 1396, "name": "Breaking Bad", "year": 2008},
            ],
        )
        await catalogs_repo.insert_watch_history_ignore_conflicts(
            s,
            [
                {
                    "user_id": env.uid,
                    "media_type": "movie",
                    "tmdb_id": 603,
                    "status": "watched",
                    "rating": 3.0,
                    "source": "manual",
                }
            ],
        )
        lb = await sources_repo.get_or_create_source(
            s,
            kind="lb_profile_csv",
            owner_user_id=env.uid,
            external_key="profile",
            label="Letterboxd Export",
            url=None,
        )
        await sources_service.subscribe_user_to_source(s, user_id=env.uid, source=lb, role="self")
        await sources_repo.upsert_source_items(
            s,
            [
                {
                    "source_id": lb.id,
                    "media_type": "series",
                    "tmdb_id": 1396,
                    "item_kind": "watched",
                    "rating": 7.0,
                    "watched_at": None,
                    "entry_url": None,
                }
            ],
        )
        await sources_service.resync_watch_history(s, user_id=env.uid)
        await s.commit()
        lb_id = lb.id

    await _run_first_sync(env, simkl)
    history = await env.history()
    # The manual row is untouched. Letterboxd subscribed after Simkl, but it wrote Breaking Bad
    # first, so Simkl's rating for it doesn't take over.
    assert (history[("movie", 603)].rating, history[("movie", 603)].source_id) == (3.0, None)
    assert history[("series", 1396)].rating == 7.0
    assert history[("series", 1396)].source_id == lb_id
    # Simkl still wrote the titles nobody else had.
    assert history[("series", 1438)].source_id == env.source_id
    # Its own item rows hold Simkl's values regardless.
    assert (await env.items())[("series", 1396)] == ("watched", 10.0)


async def test_a_later_letterboxd_export_does_not_overwrite_what_simkl_wrote(
    env: Env, simkl: SimklMock
):
    await _run_first_sync(env, simkl)
    async with env.factory() as s:
        lb = await sources_repo.get_or_create_source(
            s,
            kind="lb_profile_csv",
            owner_user_id=env.uid,
            external_key="profile",
            label="Letterboxd Export",
            url=None,
        )
        await sources_service.subscribe_user_to_source(s, user_id=env.uid, source=lb, role="self")
        await sources_repo.upsert_source_items(
            s,
            [
                {
                    "source_id": lb.id,
                    "media_type": "series",
                    "tmdb_id": 1396,
                    "item_kind": "watched",
                    "rating": 6.0,
                    "watched_at": None,
                    "entry_url": None,
                }
            ],
        )
        await sources_service.resync_watch_history(s, user_id=env.uid)
        await s.commit()
    row = (await env.history())[("series", 1396)]
    assert (row.rating, row.source, row.source_id) == (10.0, "simkl", env.source_id)


# --- Routes ------------------------------------------------------------------------------------


async def test_sync_route_queues_one_job_and_reports_conflicts(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_route_user")
    await login(committed_client, user.login_token)
    assert (await committed_client.post("/api/simkl/sync")).status_code == 404  # not connected
    await seed_connection(audit_session_factory, keypair, user.user.id)

    first = await committed_client.post("/api/simkl/sync")
    assert first.status_code == 202
    body = first.json()
    assert (body["job_kind"], body["status"], body["preset_id"]) == ("simkl_sync", "queued", None)
    assert set(body) >= {"id", "trigger", "paused_key_type", "paused_reason", "created_at"}
    again = await committed_client.post("/api/simkl/sync")
    assert again.status_code == 409
    assert "already in progress" in again.json()["detail"]

    async with audit_session_factory() as s:
        await s.execute(text("UPDATE jobs SET status = 'succeeded'"))
        await s.execute(text("UPDATE simkl_connections SET needs_reconnect = true"))
        await s.commit()
    assert (await committed_client.post("/api/simkl/sync")).status_code == 409


async def test_sync_route_needs_a_login(committed_client: httpx.AsyncClient):
    assert (await committed_client.post("/api/simkl/sync")).status_code == 401
    assert (await committed_client.get("/api/simkl/connection")).status_code == 401
    assert (await committed_client.delete("/api/simkl/connection")).status_code == 401


async def test_the_simkl_source_is_fixed_to_self_and_is_removed_by_disconnecting(
    env: Env, simkl: SimklMock
):
    sid = str(env.source_id)
    patch = await env.client.patch(f"/api/sources/{sid}", json={"role": "taste"})
    assert patch.status_code == 400
    delete = await env.client.delete(f"/api/sources/{sid}")
    assert delete.status_code == 400
    refresh = await env.client.post(f"/api/sources/{sid}/refresh")
    assert refresh.status_code == 400
    # Nothing changed, so the tokens are still there to be revoked on disconnect.
    assert (await env.connection()).source_id == env.source_id
    listed = (await env.client.get("/api/sources")).json()["sources"]
    assert [(s["kind"], s["role"], s["id"]) for s in listed] == [("simkl", "self", sid)]


# --- Users are isolated -----------------------------------------------------------------------


async def test_one_users_connection_is_invisible_to_another(
    env: Env,
    committed_app: FastAPI,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    await _run_first_sync(env, simkl)
    bob = await make_committed_user("simkl_other_user")
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=committed_app), base_url="http://t")
    try:
        await login(other, bob.login_token)
        status = (await other.get("/api/simkl/connection")).json()
        assert status["connected"] is False and status["account_name"] is None
        assert (await other.get("/api/sources")).json()["sources"] == []
        assert (await other.post("/api/simkl/sync")).status_code == 404
        assert (await other.delete("/api/simkl/connection")).status_code == 404
        sid = str(env.source_id)
        assert (await other.patch(f"/api/sources/{sid}", json={"role": "taste"})).status_code == 404
        assert (await other.delete(f"/api/sources/{sid}")).status_code == 404
        assert (await other.get("/api/settings/audit")).json()["entries"] == []
    finally:
        await other.aclose()
    # Alice's data is intact.
    assert (await env.client.get("/api/simkl/connection")).json()["connected"] is True
    assert len(await env.items()) == 9


async def test_the_database_allows_one_active_sync_per_user(env: Env, simkl: SimklMock):
    """Two requests racing past the `has_active_job` check can't both queue a sync."""
    async with env.factory() as s:
        await jobs_service.enqueue_job(s, user_id=env.uid, job_kind="simkl_sync")
        await s.commit()
    async with env.factory() as s:
        with pytest.raises(jobs_service.DuplicateJobError, match="Simkl sync is already"):
            await jobs_service.enqueue_job(s, user_id=env.uid, job_kind="simkl_sync")
    # A finished sync doesn't block the next one.
    async with env.factory() as s:
        await s.execute(
            text("UPDATE jobs SET status = 'succeeded' WHERE user_id = :u"), {"u": env.uid}
        )
        await jobs_service.enqueue_job(s, user_id=env.uid, job_kind="simkl_sync")
        await s.commit()
