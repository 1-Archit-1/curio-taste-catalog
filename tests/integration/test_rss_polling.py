"""Scheduled polling of public Letterboxd diary RSS sources, and manual refresh persistence."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients import letterboxd
from curio.db.models.catalogs import MediaItem, WatchHistory
from curio.db.models.sources import Source
from curio.repositories import sources as sources_repo
from curio.services import sources as sources_service
from curio.services.users import ProvisionedUser
from curio.settings import Settings
from curio.worker import run as run_worker
from tests.integration.lb_support import LB_FIXTURES, RSS_URL, login

pytestmark = pytest.mark.usefixtures("clean_lb_data")

MakeUser = Callable[[str], Awaitable[ProvisionedUser]]
SessionFactory = async_sessionmaker[AsyncSession]


def _settings(database_url: str) -> Settings:
    return Settings(_env_file=None, database_url=database_url)


async def _seed_source(
    factory: SessionFactory,
    subscribers: list[tuple[ProvisionedUser, str]],
    *,
    username: str = "alice",
) -> UUID:
    async with factory() as s:
        source = await sources_repo.get_or_create_source(
            s,
            kind="lb_diary_rss",
            owner_user_id=None,
            external_key=username,
            label=f"Letterboxd RSS (@{username})",
            url=f"https://letterboxd.com/{username}/rss/",
        )
        for user, role in subscribers:
            await sources_service.subscribe_user_to_source(
                s, user_id=user.user.id, source=source, role=role
            )
        await s.commit()
        return source.id


async def _source(factory: SessionFactory, source_id: UUID) -> Source:
    async with factory() as s:
        source = await s.scalar(select(Source).where(Source.id == source_id))
    assert source is not None
    return source


async def _poll(factory: SessionFactory, database_url: str, max_sources: int = 3) -> int:
    return await sources_service.poll_due_rss_sources(
        factory, settings=_settings(database_url), max_sources=max_sources
    )


async def _make_due(factory: SessionFactory, source_id: UUID) -> None:
    async with factory() as s:
        await s.execute(
            text("UPDATE sources SET next_refresh_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": source_id},
        )
        await s.commit()


def _rss_xml() -> str:
    return (LB_FIXTURES / "diary_rss.xml").read_text(encoding="utf-8")


async def test_poller_refreshes_a_due_source_once_and_syncs_self_subscribers(
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    alice = await make_committed_user("poll_alice")
    bob = await make_committed_user("poll_bob")
    route = respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text=_rss_xml()))
    source_id = await _seed_source(audit_session_factory, [(alice, "self"), (bob, "taste")])

    before = datetime.now(UTC)
    assert await _poll(audit_session_factory, migrated_database_url) == 1

    assert route.call_count == 1
    source = await _source(audit_session_factory, source_id)
    assert (source.item_count, source.last_error) == (2, None)
    assert source.last_refreshed_at is not None
    assert source.last_refreshed_at >= before
    assert source.next_refresh_at is not None
    assert (
        before + timedelta(hours=6) - timedelta(minutes=1)
        <= source.next_refresh_at
        <= datetime.now(UTC) + timedelta(hours=6)
    )
    async with audit_session_factory() as s:
        rows = list(await s.scalars(select(WatchHistory)))
        stubs = list(await s.scalars(select(MediaItem).where(MediaItem.tmdb_id.in_([603, 1396]))))
    assert {(r.user_id, r.tmdb_id, r.rating, r.source_id) for r in rows} == {
        (alice.user.id, 603, 10.0, source_id),
        (alice.user.id, 1396, 9.0, source_id),
    }
    assert [m.hydrated_at for m in stubs] == [None, None]

    # Nothing is due until the interval has passed.
    assert await _poll(audit_session_factory, migrated_database_url) == 0
    assert route.call_count == 1
    await _make_due(audit_session_factory, source_id)
    assert await _poll(audit_session_factory, migrated_database_url) == 1
    assert route.call_count == 2


async def test_poller_skips_sources_nobody_follows_and_respects_the_batch_size(
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("poll_batch_user")
    routes = {
        name: respx_mock.get(f"https://letterboxd.com/{name}/rss/").mock(
            return_value=httpx.Response(200, text=_rss_xml())
        )
        for name in ("one", "two", "three", "orphan")
    }
    for name in ("one", "two", "three"):
        await _seed_source(audit_session_factory, [(user, "taste")], username=name)
    await _seed_source(audit_session_factory, [], username="orphan")

    assert await _poll(audit_session_factory, migrated_database_url, max_sources=2) == 2
    assert sum(r.call_count for r in routes.values()) == 2
    assert await _poll(audit_session_factory, migrated_database_url, max_sources=2) == 1
    assert [routes[n].call_count for n in ("one", "two", "three", "orphan")] == [1, 1, 1, 0]


async def test_concurrent_pollers_claim_a_due_source_once(
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("poll_race_user")
    route = respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text=_rss_xml()))
    await _seed_source(audit_session_factory, [(user, "self")])

    claimed = await asyncio.gather(
        _poll(audit_session_factory, migrated_database_url),
        _poll(audit_session_factory, migrated_database_url),
    )

    assert sorted(claimed) == [0, 1]
    assert route.call_count == 1


async def test_a_source_locked_by_another_poller_is_skipped_not_waited_for(
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("poll_lock_user")
    source_id = await _seed_source(audit_session_factory, [(user, "self")])
    horizon = datetime.now(UTC) + timedelta(hours=1)

    async with audit_session_factory() as holder, audit_session_factory() as other:
        first = await sources_repo.claim_due_rss_source(holder, next_refresh_at=horizon)
        assert first is not None
        assert first.id == source_id
        async with asyncio.timeout(5):
            second = await sources_repo.claim_due_rss_source(other, next_refresh_at=horizon)
        assert second is None
        await holder.rollback()


async def test_poll_failures_are_recorded_on_a_separate_session_and_retried_later(
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("poll_fail_user")
    route = respx_mock.get(RSS_URL).mock(return_value=httpx.Response(500))
    source_id = await _seed_source(audit_session_factory, [(user, "self")])

    assert await _poll(audit_session_factory, migrated_database_url) == 1
    source = await _source(audit_session_factory, source_id)
    assert source.last_error == "Letterboxd returned HTTP 500."
    assert source.next_refresh_at is not None
    assert source.next_refresh_at > datetime.now(UTC)
    assert source.last_refreshed_at is None
    # A failing feed waits for its next slot instead of being hit on every loop.
    assert await _poll(audit_session_factory, migrated_database_url) == 0
    assert route.call_count == 1

    # An unexpected error inside the ingest is recorded too, without its details.
    await _make_due(audit_session_factory, source_id)
    route.mock(return_value=httpx.Response(200, text=_rss_xml()))

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret detail")

    with patch.object(sources_service, "ingest_rss_feed", boom):
        assert await _poll(audit_session_factory, migrated_database_url) == 1
    source = await _source(audit_session_factory, source_id)
    assert source.last_error == "Unexpected error while refreshing. See worker logs."
    async with audit_session_factory() as s:
        assert list(await s.scalars(select(WatchHistory))) == []

    # The next successful poll clears the error.
    await _make_due(audit_session_factory, source_id)
    assert await _poll(audit_session_factory, migrated_database_url) == 1
    source = await _source(audit_session_factory, source_id)
    assert (source.last_error, source.item_count) == (None, 2)


async def test_the_worker_loop_polls_due_sources(
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("poll_worker_user")
    route = respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text=_rss_xml()))
    source_id = await _seed_source(audit_session_factory, [(user, "self")])
    settings = Settings(
        _env_file=None, database_url=migrated_database_url, worker_heartbeat_seconds=0.05
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(settings, stop))
    try:
        for _ in range(200):
            if (await _source(audit_session_factory, source_id)).last_refreshed_at is not None:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the worker did not poll the source within 10 s")
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert route.call_count == 1


async def test_manual_refresh_failure_is_recorded_and_success_schedules_the_next_poll(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    respx_mock: respx.MockRouter,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("refresh_user")
    await login(committed_client, user.login_token)
    route = respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text=_rss_xml()))
    added = await committed_client.post("/api/sources", json={"input": "alice", "role": "self"})
    assert added.status_code == 201
    source_id = UUID(added.json()["id"])
    first_next = (await _source(audit_session_factory, source_id)).next_refresh_at
    assert first_next is not None

    with patch.object(letterboxd, "MAX_RSS_BYTES", 200):
        too_big = await committed_client.post(f"/api/sources/{source_id}/refresh")
    assert too_big.status_code == 400
    assert too_big.json()["detail"] == "Letterboxd RSS feed is too large."
    failed = await _source(audit_session_factory, source_id)
    assert failed.last_error == "Letterboxd RSS feed is too large."
    assert (await committed_client.get("/api/sources")).json()["sources"][0]["last_error"] == (
        "Letterboxd RSS feed is too large."
    )

    async with audit_session_factory() as s:
        await s.execute(
            text("UPDATE sources SET next_refresh_at = now() WHERE id = :i"), {"i": source_id}
        )
        await s.commit()
    ok = await committed_client.post(f"/api/sources/{source_id}/refresh")
    assert ok.status_code == 200
    assert ok.json()["last_error"] is None
    refreshed = await _source(audit_session_factory, source_id)
    assert refreshed.next_refresh_at is not None
    assert refreshed.next_refresh_at > datetime.now(UTC) + timedelta(hours=5)
    assert route.call_count == 3
