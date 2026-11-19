"""Integration tests for Issue #2: users, auth, per-user settings, encryption, audit, CORS."""

import logging

import httpx
import psycopg
import pytest

from curio.api.app import create_app
from curio.api.deps import get_conn
from curio.cli import main as cli_main
from curio.clients.providers import KeyType
from curio.crypto import (
    KeyDecryptionError,
    Keypair,
    PrivateKeyNotConfiguredError,
    generate_keypair,
)
from curio.repositories import keys as keys_repo
from curio.services import keys as keys_service
from curio.services import users as users_service
from curio.settings import Settings


def _mock_all_providers_ok(respx_mock) -> None:
    respx_mock.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=httpx.Response(200, json={"images": {}})
    )
    respx_mock.get("https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(200, json={"models": []})
    )
    respx_mock.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get("https://openrouter.ai/api/v1/auth/key").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )


async def _make_client_for_conn(
    conn: psycopg.AsyncConnection,
    database_url: str,
    keypair: Keypair,
) -> httpx.AsyncClient:
    """Create a fresh AsyncClient with its own cookie jar sharing `conn`."""
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            key_public_key=keypair.public_key,
            key_private_key=None,
        )
    )

    async def test_conn():
        yield conn

    app.dependency_overrides[get_conn] = test_conn
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_control_plane_rejects_unauthenticated_requests(client: httpx.AsyncClient):
    endpoints = [
        ("GET", "/api/auth/me", None),
        ("POST", "/api/auth/logout", None),
        ("GET", "/api/settings", None),
        ("PATCH", "/api/settings", {"llm_provider": "openai", "monthly_token_budget": 1000}),
        ("POST", "/api/settings/addon-token/rotate", None),
        ("POST", "/api/settings/keys/gemini/test", {"api_key": "test"}),
        ("PUT", "/api/settings/keys/gemini", {"api_key": "test"}),
        ("DELETE", "/api/settings/keys/gemini", None),
        ("GET", "/api/settings/audit", None),
    ]
    for method, path, body in endpoints:
        resp = await client.request(method, path, json=body)
        assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"


async def test_login_session_lifecycle(
    conn: psycopg.AsyncConnection,
    client: httpx.AsyncClient,
):
    provisioned = await users_service.create_user_with_login_token(conn, username="alice")

    # Wrong token is rejected.
    bad = await client.post("/api/auth/login", json={"token": "not-a-valid-token"})
    assert bad.status_code == 401

    # Valid token sets HttpOnly session cookie.
    resp = await client.post("/api/auth/login", json={"token": provisioned.login_token})
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "alice"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "curio_session=" in set_cookie
    assert "httponly" in set_cookie.lower()

    # Authenticated request succeeds.
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"

    # Logout revokes session.
    out = await client.post("/api/auth/logout")
    assert out.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_settings_tab_html_contains_notice_and_all_providers(client: httpx.AsyncClient):
    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "server operator can technically access stored keys" in html.lower()
    assert "dedicated gemini api key restricted to the gemini api with a quota cap" in html.lower()
    for provider in ("tmdb", "gemini", "openai", "anthropic", "openrouter"):
        assert provider in html.lower()


async def test_keys_test_encrypt_store_no_leak_and_process_split(
    conn: psycopg.AsyncConnection,
    client: httpx.AsyncClient,
    keypair: Keypair,
    respx_mock,
    caplog: pytest.LogCaptureFixture,
):
    _mock_all_providers_ok(respx_mock)
    provisioned = await users_service.create_user_with_login_token(conn, username="kuser")
    await client.post("/api/auth/login", json={"token": provisioned.login_token})

    # Test endpoint validates without storing.
    test_resp = await client.post(
        "/api/settings/keys/gemini/test",
        json={"api_key": "AIzaSyTestOnly9999"},
    )
    assert test_resp.status_code == 200
    assert test_resp.json()["ok"] is True
    assert (await client.get("/api/settings")).json()["keys"]["gemini"]["configured"] is False

    # Invalid key is rejected before storing.
    respx_mock.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    bad_save = await client.put(
        "/api/settings/keys/openai",
        json={"api_key": "sk-bad-openai-key-0000"},
    )
    assert bad_save.status_code == 422
    assert (await client.get("/api/settings")).json()["keys"]["openai"]["configured"] is False

    # Re-mock OpenAI 200 and save all 5 providers.
    respx_mock.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    secrets_by_type = {
        "tmdb": "tmdb-secret-value-1111",
        "gemini": "AIzaSySecretGemini2222",
        "openai": "sk-proj-secret-openai-3333",
        "anthropic": "sk-ant-secret-claude-4444",
        "openrouter": "sk-or-v1-secret-router-5555",
    }

    with caplog.at_level(logging.DEBUG):
        for key_type, secret in secrets_by_type.items():
            save_resp = await client.put(
                f"/api/settings/keys/{key_type}",
                json={"api_key": secret},
            )
            assert save_resp.status_code == 200
            body = save_resp.json()
            assert body["configured"] is True
            assert body["last4"] == secret[-4:]
            assert secret not in save_resp.text

        settings_resp = await client.get("/api/settings")
        assert settings_resp.status_code == 200
        settings_text = settings_resp.text
        for secret in secrets_by_type.values():
            assert secret not in settings_text

    # No plaintext secret ever appeared in logs.
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    for secret in secrets_by_type.values():
        assert secret not in log_text

    # Database holds ciphertext only, never plaintext.
    for key_type, secret in secrets_by_type.items():
        row = await keys_repo.get_encrypted_api_key(
            conn,
            user_id=provisioned.user.id,
            key_type=key_type,
        )
        assert row is not None
        assert secret.encode() not in bytes(row.encrypted_key)

    # Process-split proof: API settings (no KEY_PRIVATE_KEY) cannot decrypt.
    api_settings = Settings(
        _env_file=None,
        database_url="postgresql://unused",
        key_public_key=keypair.public_key,
        key_private_key=None,
    )
    with pytest.raises(PrivateKeyNotConfiguredError):
        await keys_service.get_llm_key(
            conn,
            user_id=provisioned.user.id,
            job_id="job-1",
            reason="catalog generation",
            private_key=api_settings.key_private_key,
        )

    # Worker (with KEY_PRIVATE_KEY) decrypts TMDB and each LLM provider and writes audit entries.
    tmdb_plain = await keys_service.get_tmdb_key(
        conn,
        user_id=provisioned.user.id,
        job_id="job-100",
        reason="enrich candidates",
        private_key=keypair.private_key,
    )
    assert tmdb_plain == secrets_by_type["tmdb"]

    for provider in (KeyType.GEMINI, KeyType.OPENAI, KeyType.ANTHROPIC, KeyType.OPENROUTER):
        patch_resp = await client.patch(
            "/api/settings",
            json={"llm_provider": provider.value, "monthly_token_budget": 500_000},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["llm_provider"] == provider.value

        resolved = await keys_service.get_llm_key(
            conn,
            user_id=provisioned.user.id,
            job_id=f"job-{provider.value}",
            reason=f"run with {provider.value}",
            private_key=keypair.private_key,
        )
        assert resolved.provider is provider
        assert resolved.api_key == secrets_by_type[provider.value]

    # Audit log endpoint shows all 5 decryptions for this user.
    audit_resp = await client.get("/api/settings/audit")
    assert audit_resp.status_code == 200
    entries = audit_resp.json()["entries"]
    assert len(entries) == 5
    assert {e["key_type"] for e in entries} == set(secrets_by_type.keys())
    assert {e["job_id"] for e in entries} == {
        "job-100",
        "job-gemini",
        "job-openai",
        "job-anthropic",
        "job-openrouter",
    }


async def test_keypair_rotation_reencrypts_all_stored_keys(
    conn: psycopg.AsyncConnection,
    keypair: Keypair,
    respx_mock,
):
    _mock_all_providers_ok(respx_mock)
    u1 = await users_service.create_user_with_login_token(conn, username="rot1")
    u2 = await users_service.create_user_with_login_token(conn, username="rot2")

    await keys_service.set_user_api_key(
        conn,
        user_id=u1.user.id,
        key_type=KeyType.GEMINI,
        api_key="AIza-rot-user1-key",
        public_key=keypair.public_key,
    )
    await keys_service.set_user_api_key(
        conn,
        user_id=u2.user.id,
        key_type=KeyType.OPENROUTER,
        api_key="sk-or-rot-user2-key",
        public_key=keypair.public_key,
    )

    new_kp = generate_keypair()
    rotated_count = await keys_service.rotate_all_keys(
        conn,
        old_private_key=keypair.private_key,
        new_public_key=new_kp.public_key,
    )
    assert rotated_count == 2

    # Old private key can no longer decrypt.
    with pytest.raises(KeyDecryptionError):
        await keys_service.get_user_api_key(
            conn,
            user_id=u1.user.id,
            key_type=KeyType.GEMINI,
            job_id="job-old",
            reason="should fail",
            private_key=keypair.private_key,
        )

    # New private key decrypts every rotated key cleanly.
    plain1 = await keys_service.get_user_api_key(
        conn,
        user_id=u1.user.id,
        key_type=KeyType.GEMINI,
        job_id="job-new-1",
        reason="post-rotation check",
        private_key=new_kp.private_key,
    )
    plain2 = await keys_service.get_user_api_key(
        conn,
        user_id=u2.user.id,
        key_type=KeyType.OPENROUTER,
        job_id="job-new-2",
        reason="post-rotation check",
        private_key=new_kp.private_key,
    )
    assert plain1 == "AIza-rot-user1-key"
    assert plain2 == "sk-or-rot-user2-key"


async def test_multi_user_isolation(
    conn: psycopg.AsyncConnection,
    migrated_database_url: str,
    keypair: Keypair,
    respx_mock,
):
    _mock_all_providers_ok(respx_mock)
    alice = await users_service.create_user_with_login_token(conn, username="iso_alice")
    bob = await users_service.create_user_with_login_token(conn, username="iso_bob")

    async with (
        await _make_client_for_conn(conn, migrated_database_url, keypair) as alice_client,
        await _make_client_for_conn(conn, migrated_database_url, keypair) as bob_client,
    ):
        assert (
            await alice_client.post("/api/auth/login", json={"token": alice.login_token})
        ).status_code == 200
        assert (
            await bob_client.post("/api/auth/login", json={"token": bob.login_token})
        ).status_code == 200

        # Both users save overlapping key types and preferences.
        assert (
            await alice_client.put("/api/settings/keys/gemini", json={"api_key": "AIza-alice-1111"})
        ).status_code == 200
        assert (
            await bob_client.put("/api/settings/keys/gemini", json={"api_key": "AIza-bob-9999"})
        ).status_code == 200
        assert (
            await bob_client.patch(
                "/api/settings", json={"llm_provider": "anthropic", "monthly_token_budget": 12345}
            )
        ).status_code == 200

        # Worker decrypts for both users.
        await keys_service.get_llm_key(
            conn,
            user_id=alice.user.id,
            job_id="alice-job",
            reason="alice run",
            private_key=keypair.private_key,
        )
        await keys_service.get_llm_key(
            conn,
            user_id=bob.user.id,
            job_id="bob-job",
            reason="bob run",
            private_key=keypair.private_key,
            provider=KeyType.GEMINI,
        )

        # Each user sees only their own settings, keys, manifest URL, and audit log.
        a_settings = (await alice_client.get("/api/settings")).json()
        b_settings = (await bob_client.get("/api/settings")).json()
        assert a_settings["username"] == "iso_alice"
        assert a_settings["keys"]["gemini"]["last4"] == "1111"
        assert a_settings["llm_provider"] == "gemini"
        assert a_settings["addon_manifest_url"] != b_settings["addon_manifest_url"]

        assert b_settings["username"] == "iso_bob"
        assert b_settings["keys"]["gemini"]["last4"] == "9999"
        assert b_settings["llm_provider"] == "anthropic"
        assert b_settings["monthly_token_budget"] == 12345

        a_audit = (await alice_client.get("/api/settings/audit")).json()["entries"]
        b_audit = (await bob_client.get("/api/settings/audit")).json()["entries"]
        assert [e["job_id"] for e in a_audit] == ["alice-job"]
        assert [e["job_id"] for e in b_audit] == ["bob-job"]

        # Alice deleting her key and rotating her addon token does not affect Bob.
        assert (await alice_client.delete("/api/settings/keys/gemini")).json()["deleted"] is True
        assert (await alice_client.post("/api/settings/addon-token/rotate")).status_code == 200

        b_after = (await bob_client.get("/api/settings")).json()
        assert b_after["keys"]["gemini"]["configured"] is True
        assert b_after["keys"]["gemini"]["last4"] == "9999"
        assert b_after["addon_manifest_url"] == b_settings["addon_manifest_url"]


async def test_cors_star_present_on_addon_routes_only(
    conn: psycopg.AsyncConnection,
    client: httpx.AsyncClient,
):
    provisioned = await users_service.create_user_with_login_token(conn, username="cors_user")
    origin = {"Origin": "https://app.strem.io"}

    # Addon GET (valid token) has CORS `*`.
    manifest = await client.get(f"/{provisioned.user.addon_token}/manifest.json", headers=origin)
    assert manifest.status_code == 200
    assert manifest.headers.get("access-control-allow-origin") == "*"

    # Addon GET (invalid token) still has CORS `*` and returns 404.
    missing = await client.get("/bogus-token/manifest.json", headers=origin)
    assert missing.status_code == 404
    assert missing.headers.get("access-control-allow-origin") == "*"

    # Addon OPTIONS preflight has CORS `*`.
    preflight = await client.options(
        f"/{provisioned.user.addon_token}/manifest.json", headers=origin
    )
    assert preflight.status_code == 204
    assert preflight.headers.get("access-control-allow-origin") == "*"

    # Control-plane, health, and UI routes have NO CORS headers.
    await client.post("/api/auth/login", json={"token": provisioned.login_token})
    for path in ("/api/auth/me", "/api/settings", "/healthz", "/"):
        r = await client.get(path, headers=origin)
        assert "access-control-allow-origin" not in r.headers, f"unexpected CORS on {path}"


def test_cli_user_creation_and_addon_token_rotation(
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setenv("DATABASE_URL", migrated_database_url)
    username = "cli_test_user"

    try:
        assert cli_main(["keygen"]) == 0
        out = capsys.readouterr().out
        assert "KEY_PUBLIC_KEY=" in out
        assert "KEY_PRIVATE_KEY=" in out

        assert cli_main(["users", "create", username]) == 0
        create_out = capsys.readouterr().out
        assert f"Created user: {username}" in create_out
        assert "Login token" in create_out

        # Extract initial addon path.
        old_path = next(
            line.split(": ", 1)[1].strip()
            for line in create_out.splitlines()
            if line.startswith("Addon manifest path:")
        )

        assert cli_main(["users", "rotate-addon-token", username]) == 0
        rot_out = capsys.readouterr().out
        new_path = next(
            line.split(": ", 1)[1].strip()
            for line in rot_out.splitlines()
            if line.startswith("New addon manifest path:")
        )
        assert new_path != old_path
        assert new_path.endswith("/manifest.json")
    finally:
        # Clean up committed CLI test row from the session test database.
        with psycopg.connect(migrated_database_url, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM users WHERE username = %s", (username,))
