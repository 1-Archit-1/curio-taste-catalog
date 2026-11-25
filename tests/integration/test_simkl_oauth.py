"""Simkl AUTH V2: connect, the callback checks, token storage, and reconnecting."""

import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.providers import KeyType
from curio.crypto import KeyDecryptor, Keypair, hash_token
from curio.db.models.jobs import Job
from curio.db.models.simkl import SimklConnection, SimklOAuthState, SimklRevocation
from curio.db.models.sources import Source, UserSource
from curio.services import keys as keys_service
from curio.services import simkl as simkl_service
from curio.services.users import ProvisionedUser
from tests.integration.lb_support import login
from tests.integration.simkl_support import (
    ACCESS,
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    REFRESH,
    SimklMock,
    assert_secrets_absent,
    configure_api,
    delete_test_titles,
    query_of,
    seed_connection,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

MakeUser = Callable[[str], Awaitable[ProvisionedUser]]
SessionFactory = async_sessionmaker[AsyncSession]
ISS = "https://simkl.com"


@pytest.fixture(autouse=True)
async def _clean_titles(audit_session_factory: SessionFactory):
    yield
    await delete_test_titles(audit_session_factory)


@pytest.fixture
def simkl(respx_mock: respx.MockRouter, committed_app: FastAPI) -> SimklMock:
    configure_api(committed_app)
    mock = SimklMock(respx_mock)
    mock.token.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "token_type": "Bearer",
                "expires_in": 604800,
                "refresh_token": REFRESH,
                "scope": "media:read",
            },
        )
    )
    return mock


async def _start(client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    resp = await client.post("/api/simkl/connect")
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorize_url"]
    return url, query_of(url)


def _callback_query(state: str, **overrides: str | None) -> str:
    params = {"state": state, "iss": ISS, "code": "auth-code-123", **overrides}
    return urlencode({k: v for k, v in params.items() if v is not None})


async def _callback(client: httpx.AsyncClient, state: str, **overrides: str | None):
    return await client.get(f"/api/simkl/callback?{_callback_query(state, **overrides)}")


def _outcome(resp: httpx.Response) -> str:
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/app/sources?simkl=")
    return location.removeprefix("/app/sources?simkl=")


async def _login_new(
    app: FastAPI, make_user: MakeUser, name: str
) -> tuple[httpx.AsyncClient, ProvisionedUser]:
    provisioned = await make_user(name)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    await login(client, provisioned.login_token)
    return client, provisioned


async def test_connect_needs_a_login_and_a_configured_app(
    committed_client: httpx.AsyncClient,
    committed_app: FastAPI,
    make_committed_user: MakeUser,
):
    assert (await committed_client.post("/api/simkl/connect")).status_code == 401
    user = await make_committed_user("simkl_cfg_user")
    await login(committed_client, user.login_token)
    resp = await committed_client.post("/api/simkl/connect")
    assert resp.status_code == 503
    assert "SIMKL_CLIENT_ID" in resp.json()["detail"]
    status = (await committed_client.get("/api/simkl/connection")).json()
    assert status["configured"] is False and status["connected"] is False
    configure_api(committed_app, public_base_url=None)  # no redirect URI to send
    assert (await committed_client.post("/api/simkl/connect")).status_code == 503
    configure_api(committed_app, key_public_key=None)
    assert (await committed_client.post("/api/simkl/connect")).status_code == 503


async def test_connect_builds_a_pkce_authorize_url_and_stores_only_a_hashed_state(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_connect_user")
    await login(committed_client, user.login_token)
    # The browser can't choose where Simkl sends the user back.
    resp = await committed_client.post(
        "/api/simkl/connect?redirect_uri=https://evil.example/cb",
        json={"redirect_uri": "https://evil.example/cb"},
    )
    assert resp.status_code == 200
    url = resp.json()["authorize_url"]
    assert url.startswith("https://simkl.com/oauth2/authorize?")
    query = query_of(url)
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["response_type"] == "code"
    assert query["scope"] == "media:read"
    assert query["code_challenge_method"] == "S256"
    assert CLIENT_SECRET not in url
    async with audit_session_factory() as s:
        states = list(await s.scalars(select(SimklOAuthState)))
    (row,) = [r for r in states if r.user_id == user.user.id]
    assert row.state_hash == hash_token(query["state"]) != query["state"]
    assert row.session_hash == hash_token(committed_client.cookies["curio_session"])
    challenge = base64.urlsafe_b64encode(hashlib.sha256(row.code_verifier.encode()).digest())
    assert challenge.rstrip(b"=").decode() == query["code_challenge"]
    assert 43 <= len(row.code_verifier) <= 128
    assert timedelta(minutes=9) < row.expires_at - datetime.now(UTC) <= timedelta(minutes=10)


async def test_redirect_uri_setting_overrides_the_public_base_url(
    committed_client: httpx.AsyncClient,
    committed_app: FastAPI,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    configure_api(committed_app, simkl_redirect_uri="http://localhost:8000/api/simkl/callback")
    user = await make_committed_user("simkl_uri_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    assert query["redirect_uri"] == "http://localhost:8000/api/simkl/callback"


async def test_successful_callback_stores_sealed_tokens_and_queues_a_sync(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
    caplog: pytest.LogCaptureFixture,
):
    user = await make_committed_user("simkl_ok_user")
    uid = user.user.id
    await login(committed_client, user.login_token)
    url, query = await _start(committed_client)
    with caplog.at_level(logging.DEBUG):
        resp = await _callback(committed_client, query["state"])
    assert _outcome(resp) == "connected"
    assert ACCESS not in resp.headers["location"] and "code" not in resp.headers["location"]

    # The exchange: PKCE verifier, the same redirect_uri, client auth as HTTP Basic.
    request = simkl.token.calls.last.request
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code-123"
    assert form["redirect_uri"] == REDIRECT_URI
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {basic}"
    assert simkl.token.call_count == 1

    async with audit_session_factory() as s:
        connection = await s.get(SimklConnection, uid)
        source = await s.get(Source, connection.source_id) if connection else None
        subscription = await s.scalar(select(UserSource).where(UserSource.user_id == uid))
        jobs = list(await s.scalars(select(Job).where(Job.user_id == uid)))
        states = list(
            await s.scalars(select(SimklOAuthState).where(SimklOAuthState.user_id == uid))
        )
    assert connection is not None and source is not None and subscription is not None
    assert (source.kind, source.owner_user_id, source.external_key) == ("simkl", uid, "account")
    assert source.label == "Simkl (@curio-fixture-user)"
    assert (subscription.source_id, subscription.role) == (source.id, "self")
    assert (connection.simkl_account_id, connection.simkl_account_name) == (
        424242,
        "curio-fixture-user",
    )
    assert connection.scope == "media:read" and connection.needs_reconnect is False
    assert connection.activities_snapshot is None
    assert timedelta(days=6) < connection.access_expires_at - datetime.now(UTC) <= timedelta(days=7)
    assert [(j.job_kind, j.status, j.trigger, j.payload) for j in jobs] == [
        ("simkl_sync", "queued", "connect", None)
    ]
    assert jobs[0].required_keys == ["simkl"]
    assert states == []  # the state was spent

    # Sealed: only the worker's private key opens it, and no plaintext lands anywhere.
    decryptor = KeyDecryptor(keypair.private_key)
    assert decryptor.decrypt(connection.access_token_ciphertext) == ACCESS
    assert decryptor.decrypt(connection.refresh_token_ciphertext) == REFRESH
    assert ACCESS.encode() not in bytes(connection.access_token_ciphertext)
    await assert_secrets_absent(audit_session_factory, ACCESS, REFRESH, CLIENT_SECRET)
    for record in caplog.records:
        message = record.getMessage()
        assert ACCESS not in message and REFRESH not in message and CLIENT_SECRET not in message
        if record.name.startswith("curio"):
            assert "auth-code-123" not in message

    status = (await committed_client.get("/api/simkl/connection")).json()
    assert status == {
        "configured": True,
        "connected": True,
        "source_id": str(source.id),
        "account_name": "curio-fixture-user",
        "last_synced_at": None,
        "last_error": None,
        "needs_reconnect": False,
        "sync_job_id": str(jobs[0].id),  # the sync queued by connecting
    }
    listed = (await committed_client.get("/api/sources")).json()["sources"]
    assert [(s["kind"], s["role"]) for s in listed] == [("simkl", "self")]
    assert ACCESS not in str(listed) and REFRESH not in str(listed)


async def test_state_is_single_use_and_expires(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_state_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    assert _outcome(await _callback(committed_client, query["state"])) == "connected"
    # Replaying the same callback fails and doesn't reach Simkl again.
    assert _outcome(await _callback(committed_client, query["state"])) == "error"
    assert simkl.token.call_count == 1

    _, expired = await _start(committed_client)
    async with audit_session_factory() as s:
        await s.execute(
            text("UPDATE simkl_oauth_states SET expires_at = now() - interval '1 second'")
        )
        await s.commit()
    assert _outcome(await _callback(committed_client, expired["state"])) == "error"
    assert simkl.token.call_count == 1
    async with audit_session_factory() as s:
        assert await s.scalar(select(SimklOAuthState)) is None  # spent even though it failed


async def test_unknown_or_missing_state_is_rejected_without_an_exchange(
    committed_client: httpx.AsyncClient,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_nostate_user")
    await login(committed_client, user.login_token)
    assert _outcome(await _callback(committed_client, "never-issued")) == "error"
    resp = await committed_client.get(f"/api/simkl/callback?iss={ISS}&code=c")
    assert _outcome(resp) == "error"
    assert simkl.token.call_count == 0


async def test_state_is_bound_to_the_browser_session(
    committed_client: httpx.AsyncClient,
    committed_app: FastAPI,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_session_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    # The same user in another browser has another session cookie.
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=committed_app), base_url="http://t")
    async with other:
        await login(other, user.login_token)
        assert other.cookies["curio_session"] != committed_client.cookies["curio_session"]
        assert _outcome(await _callback(other, query["state"])) == "error"
    assert simkl.token.call_count == 0
    # The state is gone, so the original browser can't use it either.
    assert _outcome(await _callback(committed_client, query["state"])) == "error"
    assert simkl.token.call_count == 0


async def test_state_is_bound_to_the_user_and_needs_a_login(
    committed_client: httpx.AsyncClient,
    committed_app: FastAPI,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    alice = await make_committed_user("simkl_alice")
    await login(committed_client, alice.login_token)
    _, first = await _start(committed_client)
    _, second = await _start(committed_client)
    bob_client, _ = await _login_new(committed_app, make_committed_user, "simkl_bob")
    try:
        assert _outcome(await _callback(bob_client, first["state"])) == "error"
    finally:
        await bob_client.aclose()
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=committed_app), base_url="http://t"
    )
    async with anonymous:
        assert _outcome(await _callback(anonymous, second["state"])) == "error"
    assert simkl.token.call_count == 0


@pytest.mark.parametrize(
    "iss",
    [None, "", "https://evil.example", "https://simkl.com.evil.example", "https://simkl.com/"],
)
async def test_a_wrong_or_missing_issuer_is_rejected_before_the_exchange(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
    simkl: SimklMock,
    iss: str | None,
):
    user = await make_committed_user("simkl_iss_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    assert _outcome(await _callback(committed_client, query["state"], iss=iss)) == "error"
    assert simkl.token.call_count == 0
    async with audit_session_factory() as s:
        assert await s.get(SimklConnection, user.user.id) is None
        assert await s.scalar(select(SimklOAuthState)) is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [("access_denied", "cancelled"), ("server_error", "error"), ("invalid_request", "error")],
)
async def test_an_error_callback_ends_the_flow_without_an_exchange(
    committed_client: httpx.AsyncClient,
    make_committed_user: MakeUser,
    simkl: SimklMock,
    error: str,
    expected: str,
):
    user = await make_committed_user("simkl_denied_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    resp = await _callback(committed_client, query["state"], error=error, code=None)
    assert _outcome(resp) == expected
    assert simkl.token.call_count == 0


async def test_an_error_callback_is_only_believed_with_a_valid_state_and_issuer(
    committed_client: httpx.AsyncClient,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_denied_iss_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    resp = await _callback(
        committed_client, query["state"], error="access_denied", code=None, iss="https://evil"
    )
    assert _outcome(resp) == "error"  # not "cancelled"
    assert _outcome(await _callback(committed_client, "x", error="access_denied")) == "error"


async def test_a_failed_exchange_is_not_retried_and_stores_nothing(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    simkl.token.mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    user = await make_committed_user("simkl_fail_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    assert _outcome(await _callback(committed_client, query["state"])) == "error"
    assert simkl.token.call_count == 1
    async with audit_session_factory() as s:
        assert await s.get(SimklConnection, user.user.id) is None
        assert (
            list(await s.scalars(select(Source).where(Source.owner_user_id == user.user.id))) == []
        )


async def test_a_token_that_cannot_read_is_rejected_and_revoked(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    simkl.token.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "token_type": "Bearer",
                "expires_in": 604800,
                "refresh_token": REFRESH,
                "scope": "something:else",
            },
        )
    )
    user = await make_committed_user("simkl_scope_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    assert _outcome(await _callback(committed_client, query["state"])) == "error"
    assert parse_qs(simkl.revoke.calls.last.request.content.decode()) == {"token": [REFRESH]}
    async with audit_session_factory() as s:
        assert await s.get(SimklConnection, user.user.id) is None


async def test_reconnecting_replaces_the_grant_revokes_the_old_one_and_clears_a_pause(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    keypair: Keypair,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_reconnect_user")
    uid = user.user.id
    await login(committed_client, user.login_token)
    old_refresh = "simkl_rt_OLDGRANTREFRESHTOKEN000000000000000000"
    await seed_connection(
        audit_session_factory,
        keypair,
        uid,
        access="simkl_at_OLDGRANTACCESSTOKEN00000000000000000000",
        refresh=old_refresh,
        snapshot={"all": "2025-01-01T00:00:00Z"},
    )
    # The old grant was rejected and its queued sync job is paused.
    async with audit_session_factory() as s:
        paused_job = await simkl_service.request_sync(s, user_id=uid)
        await s.commit()
        paused_job_id = paused_job.id
    async with audit_session_factory() as s:
        await keys_service.pause_key_and_jobs(
            s,
            user_id=uid,
            key_type=KeyType.SIMKL,
            reason="Simkl rejected the stored access token.",
        )
        await s.commit()
    status = (await committed_client.get("/api/simkl/connection")).json()
    assert status["needs_reconnect"] is True

    _, query = await _start(committed_client)
    assert _outcome(await _callback(committed_client, query["state"])) == "connected"

    async with audit_session_factory() as s:
        connection = await s.get(SimklConnection, uid)
        revocations = list(await s.scalars(select(SimklRevocation)))
        jobs = list(await s.scalars(select(Job).where(Job.user_id == uid)))
        sources = list(await s.scalars(select(Source).where(Source.owner_user_id == uid)))
    assert connection is not None
    assert (connection.needs_reconnect, connection.last_error) == (False, None)
    assert connection.activities_snapshot is None  # the next sync is a full pull
    assert len(sources) == 1  # same source, not a second one
    (revocation,) = [r for r in revocations if r.user_id == uid]
    assert KeyDecryptor(keypair.private_key).decrypt(revocation.refresh_token_ciphertext) == (
        old_refresh
    )
    by_kind = {j.job_kind: j for j in jobs}
    assert by_kind["simkl_revoke"].payload == {"revocation_id": str(revocation.id)}
    assert by_kind["simkl_revoke"].required_keys == []
    # The paused sync runs again; no second one is queued next to it.
    syncs = [j for j in jobs if j.job_kind == "simkl_sync"]
    assert [(j.id, j.status, j.paused_key_type) for j in syncs] == [(paused_job_id, "queued", None)]
    assert (await committed_client.get("/api/simkl/connection")).json()["needs_reconnect"] is False


async def test_callback_never_exposes_tokens_through_the_status_or_job_routes(
    committed_client: httpx.AsyncClient,
    audit_session_factory: SessionFactory,
    make_committed_user: MakeUser,
    simkl: SimklMock,
):
    user = await make_committed_user("simkl_leak_user")
    await login(committed_client, user.login_token)
    _, query = await _start(committed_client)
    await _callback(committed_client, query["state"])
    async with audit_session_factory() as s:
        job_id = await s.scalar(select(Job.id).where(Job.user_id == user.user.id))
    bodies = [
        (await committed_client.get("/api/simkl/connection")).text,
        (await committed_client.get("/api/sources")).text,
        (await committed_client.get(f"/api/jobs/{job_id}")).text,
        (await committed_client.get("/api/settings/audit")).text,
        (await committed_client.get("/api/settings")).text,
    ]
    for body in bodies:
        assert ACCESS not in body and REFRESH not in body and CLIENT_SECRET not in body
