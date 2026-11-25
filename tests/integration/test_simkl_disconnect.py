"""Disconnecting Simkl: the source goes at once, a worker revokes the grant, tokens stay sealed."""

from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.crypto import KeyDecryptor, Keypair, generate_keypair
from curio.db.models.catalogs import WatchHistory
from curio.db.models.jobs import Job
from curio.db.models.keys import KeyDecryptAudit
from curio.db.models.simkl import SimklConnection, SimklRevocation
from curio.db.models.sources import Source, SourceItem, UserSource
from curio.repositories import catalogs as catalogs_repo
from curio.repositories import simkl as simkl_repo
from curio.repositories import sources as sources_repo
from curio.services import keys as keys_service
from curio.services import sources as sources_service
from curio.services.users import ProvisionedUser
from tests.integration.lb_support import login
from tests.integration.simkl_support import (
    ACCESS,
    CLIENT_SECRET,
    REFRESH,
    SimklMock,
    assert_secrets_absent,
    configure_api,
    delete_test_titles,
    load,
    make_due,
    run_worker,
    seed_connection,
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


async def _jobs(factory: SessionFactory, uid: object, kind: str) -> list[Job]:
    async with factory() as s:
        rows = await s.scalars(select(Job).where(Job.user_id == uid, Job.job_kind == kind))
        return list(rows)


async def _revocations(factory: SessionFactory, uid: object) -> list[SimklRevocation]:
    async with factory() as s:
        rows = await s.scalars(select(SimklRevocation).where(SimklRevocation.user_id == uid))
        return list(rows)


async def _history(factory: SessionFactory, uid: object) -> dict[tuple[str, int], WatchHistory]:
    async with factory() as s:
        rows = await s.scalars(select(WatchHistory).where(WatchHistory.user_id == uid))
        return {(r.media_type, r.tmdb_id): r for r in rows}


async def test_disconnect_removes_the_source_and_its_history_and_hands_titles_back(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_disc_user")
    uid = user.user.id
    await login(committed_client, user.login_token)
    simkl_source_id = await seed_connection(audit_session_factory, keypair, uid)
    simkl.set_activities(load("activities.json"))
    assert (await committed_client.post("/api/simkl/sync")).status_code == 202
    assert await run_worker(audit_session_factory, migrated_database_url, keypair)
    before = await _history(audit_session_factory, uid)
    assert len(before) == 9 and all(h.source_id == simkl_source_id for h in before.values())

    # A Letterboxd export that also has Breaking Bad, and a manual rating.
    async with audit_session_factory() as s:
        await sources_repo.ensure_media_item_stubs(
            s,
            [
                {"media_type": "series", "tmdb_id": 900004, "name": "Mine", "year": None},
                {"media_type": "series", "tmdb_id": 900005, "name": "Manual", "year": None},
            ],
        )
        lb = await sources_repo.get_or_create_source(
            s,
            kind="lb_profile_csv",
            owner_user_id=uid,
            external_key="profile",
            label="Letterboxd Export",
            url=None,
        )
        await sources_service.subscribe_user_to_source(s, user_id=uid, source=lb, role="self")
        await sources_repo.upsert_source_items(
            s,
            [
                {
                    "source_id": lb.id,
                    "media_type": "series",
                    "tmdb_id": tmdb,
                    "item_kind": "watched",
                    "rating": rating,
                    "watched_at": None,
                    "entry_url": None,
                }
                for tmdb, rating in ((1396, 6.0), (900004, 7.0))
            ],
        )
        await catalogs_repo.insert_watch_history_ignore_conflicts(
            s,
            [
                {
                    "user_id": uid,
                    "media_type": "series",
                    "tmdb_id": 900005,
                    "status": "watched",
                    "rating": 2.0,
                    "source": "manual",
                }
            ],
        )
        await sources_service.resync_watch_history(s, user_id=uid)
        await s.commit()
        lb_id = lb.id
    owners = {k: h.source_id for k, h in (await _history(audit_session_factory, uid)).items()}
    assert owners[("series", 1396)] == simkl_source_id  # Simkl wrote it first
    assert owners[("series", 900004)] == lb_id

    # A sync that was queued but hadn't started is cancelled by the disconnect.
    assert (await committed_client.post("/api/simkl/sync")).status_code == 202

    resp = await committed_client.delete("/api/simkl/connection")
    assert resp.status_code == 200, resp.text

    async with audit_session_factory() as s:
        assert await s.get(Source, simkl_source_id) is None
        assert await s.get(SimklConnection, uid) is None
        items = await s.scalar(
            select(func.count())
            .select_from(SourceItem)
            .where(SourceItem.source_id == simkl_source_id)
        )
        subscriptions = list(await s.scalars(select(UserSource).where(UserSource.user_id == uid)))
    assert items == 0
    assert [u.source_id for u in subscriptions] == [lb_id]
    history = await _history(audit_session_factory, uid)
    # Simkl's rows are gone, Letterboxd takes over the title they shared, manual rows stay.
    assert set(history) == {("series", 1396), ("series", 900004), ("series", 900005)}
    assert (history[("series", 1396)].rating, history[("series", 1396)].source_id) == (6.0, lb_id)
    assert history[("series", 900005)].source_id is None
    assert [s["kind"] for s in (await committed_client.get("/api/sources")).json()["sources"]] == [
        "lb_profile_csv"
    ]
    status = (await committed_client.get("/api/simkl/connection")).json()
    assert status["connected"] is False and status["configured"] is True
    sync_jobs = await _jobs(audit_session_factory, uid, "simkl_sync")
    assert sorted(j.status for j in sync_jobs) == ["cancelled", "succeeded"]
    assert (await committed_client.delete("/api/simkl/connection")).status_code == 404
    assert (await committed_client.post("/api/simkl/sync")).status_code == 404

    # The API couldn't read the tokens, so a worker job holds them: sealed, payload-free.
    (revocation,) = await _revocations(audit_session_factory, uid)
    assert KeyDecryptor(keypair.private_key).decrypt(revocation.refresh_token_ciphertext) == REFRESH
    (revoke_job,) = await _jobs(audit_session_factory, uid, "simkl_revoke")
    assert (revoke_job.status, revoke_job.payload, revoke_job.required_keys) == (
        "queued",
        {"revocation_id": str(revocation.id)},
        [],
    )
    await assert_secrets_absent(audit_session_factory, ACCESS, REFRESH, CLIENT_SECRET)
    assert simkl.revoke.call_count == 0

    # The worker revokes the grant at Simkl and deletes the ciphertext.
    assert await run_worker(audit_session_factory, migrated_database_url, keypair)
    request = simkl.revoke.calls.last.request
    assert simkl.revoke.call_count == 1
    assert request.url.path == "/oauth2/revoke"
    assert parse_qs(request.content.decode()) == {"token": [REFRESH]}
    assert request.headers["authorization"].startswith("Basic ")
    assert await _revocations(audit_session_factory, uid) == []
    (done,) = await _jobs(audit_session_factory, uid, "simkl_revoke")
    assert done.status == "succeeded"
    async with audit_session_factory() as s:
        audit = list(await s.scalars(select(KeyDecryptAudit).where(KeyDecryptAudit.user_id == uid)))
    assert [(a.key_type, a.reason, a.job_id) for a in audit if a.reason == "simkl revoke"] == [
        ("simkl", "simkl revoke", str(done.id))
    ]
    entries = (await committed_client.get("/api/settings/audit")).json()["entries"]
    assert "simkl" in {e["key_type"] for e in entries}


async def test_a_failed_revoke_is_retried_and_the_token_is_discarded_after_the_last_attempt(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_revoke_fail_user")
    uid = user.user.id
    await login(committed_client, user.login_token)
    await seed_connection(audit_session_factory, keypair, uid)
    assert (await committed_client.delete("/api/simkl/connection")).status_code == 200
    simkl.revoke.mock(return_value=httpx.Response(503))
    (job,) = await _jobs(audit_session_factory, uid, "simkl_revoke")
    async with audit_session_factory() as s:
        await s.execute(text("UPDATE jobs SET max_attempts = 2 WHERE id = :id"), {"id": job.id})
        await s.commit()

    assert await run_worker(audit_session_factory, migrated_database_url, keypair)
    (retry,) = await _jobs(audit_session_factory, uid, "simkl_revoke")
    assert retry.status == "queued" and retry.last_error == "Simkl is unavailable (HTTP 503)."
    assert len(await _revocations(audit_session_factory, uid)) == 1  # still there to retry

    await make_due(audit_session_factory, job.id)
    assert await run_worker(audit_session_factory, migrated_database_url, keypair)
    (final,) = await _jobs(audit_session_factory, uid, "simkl_revoke")
    assert final.status == "failed"
    assert await _revocations(audit_session_factory, uid) == []  # not kept for ever
    assert simkl.revoke.call_count == 2


async def test_a_rejected_client_secret_fails_the_revoke_at_once_and_discards_the_token(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_revoke_secret_user")
    uid = user.user.id
    await login(committed_client, user.login_token)
    await seed_connection(audit_session_factory, keypair, uid)
    await committed_client.delete("/api/simkl/connection")
    simkl.revoke.mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    assert await run_worker(audit_session_factory, migrated_database_url, keypair)
    (job,) = await _jobs(audit_session_factory, uid, "simkl_revoke")
    assert job.status == "failed" and "SIMKL_CLIENT_SECRET" in (job.last_error or "")
    assert await _revocations(audit_session_factory, uid) == []
    assert simkl.revoke.call_count == 1


async def test_a_revoke_job_whose_row_is_gone_succeeds_without_calling_simkl(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_revoke_twice_user")
    uid = user.user.id
    await login(committed_client, user.login_token)
    await seed_connection(audit_session_factory, keypair, uid)
    await committed_client.delete("/api/simkl/connection")
    async with audit_session_factory() as s:
        await s.execute(text("DELETE FROM simkl_revocations WHERE user_id = :u"), {"u": uid})
        await s.commit()
    assert await run_worker(audit_session_factory, migrated_database_url, keypair)
    (job,) = await _jobs(audit_session_factory, uid, "simkl_revoke")
    assert job.status == "succeeded"
    assert simkl.revoke.call_count == 0


async def test_reconnecting_revokes_the_replaced_grant_in_the_worker(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    migrated_database_url: str,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_regrant_user")
    uid = user.user.id
    await login(committed_client, user.login_token)
    old_refresh = "simkl_rt_REPLACEDGRANTREFRESHTOKEN000000000000"
    await seed_connection(audit_session_factory, keypair, uid, refresh=old_refresh)
    # Walk the real flow: connect, then the callback with a new grant.
    resp = await committed_client.post("/api/simkl/connect")
    state = parse_qs(resp.json()["authorize_url"].split("?", 1)[1])["state"][0]
    callback = await committed_client.get(
        f"/api/simkl/callback?state={state}&iss=https%3A%2F%2Fsimkl.com&code=c"
    )
    assert callback.headers["location"] == "/app/sources?simkl=connected"
    while await run_worker(audit_session_factory, migrated_database_url, keypair):
        pass
    revoked = [parse_qs(c.request.content.decode())["token"][0] for c in simkl.revoke.calls]
    assert revoked == [old_refresh]  # the replaced grant, not the new one
    assert await _revocations(audit_session_factory, uid) == []
    assert [j.status for j in await _jobs(audit_session_factory, uid, "simkl_revoke")] == [
        "succeeded"
    ]


async def test_key_rotation_re_encrypts_simkl_tokens_too(
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
):
    user = await make_committed_user("simkl_rotate_user")
    uid = user.user.id
    await seed_connection(audit_session_factory, keypair, uid)
    async with audit_session_factory() as s:
        connection = await simkl_repo.get_connection(s, user_id=uid)
        assert connection is not None
        await simkl_repo.create_revocation(
            s, user_id=uid, refresh_token_ciphertext=connection.refresh_token_ciphertext
        )
        await s.commit()
    new_keypair = generate_keypair()
    async with audit_session_factory() as s:
        count = await keys_service.rotate_all_keys(
            s, old_private_key=keypair.private_key, new_public_key=new_keypair.public_key
        )
        await s.commit()
    assert count >= 2
    new = KeyDecryptor(new_keypair.private_key)
    async with audit_session_factory() as s:
        connection = await simkl_repo.get_connection(s, user_id=uid)
        (revocation,) = list(
            await s.scalars(select(SimklRevocation).where(SimklRevocation.user_id == uid))
        )
        audit = list(await s.scalars(select(KeyDecryptAudit).where(KeyDecryptAudit.user_id == uid)))
    assert connection is not None
    assert new.decrypt(connection.access_token_ciphertext) == ACCESS
    assert new.decrypt(connection.refresh_token_ciphertext) == REFRESH
    assert new.decrypt(revocation.refresh_token_ciphertext) == REFRESH
    assert [(a.key_type, a.reason) for a in audit] == [("simkl", "rotation")] * 2
