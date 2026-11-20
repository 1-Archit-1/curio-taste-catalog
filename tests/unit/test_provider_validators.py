import httpx
import pytest

from curio.clients.providers import KeyType, validate_provider_key


@pytest.mark.parametrize(
    ("key_type", "url", "api_key"),
    [
        (KeyType.TMDB, "https://api.themoviedb.org/3/configuration", "tmdb-v3-key-1234"),
        (
            KeyType.TMDB,
            "https://api.themoviedb.org/3/configuration",
            "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.signature",
        ),
        (
            KeyType.GEMINI,
            "https://generativelanguage.googleapis.com/v1beta/models",
            "AIzaSyTestGeminiKey1234",
        ),
        (KeyType.OPENAI, "https://api.openai.com/v1/models", "sk-proj-openai-test-5678"),
        (KeyType.ANTHROPIC, "https://api.anthropic.com/v1/models", "sk-ant-api03-test-9012"),
        (KeyType.OPENROUTER, "https://openrouter.ai/api/v1/auth/key", "sk-or-v1-test-3456"),
    ],
)
async def test_validate_provider_key_success(respx_mock, key_type, url, api_key):
    route = respx_mock.get(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    result = await validate_provider_key(key_type, api_key)

    assert result.ok is True
    assert result.status_code == 200
    assert route.called


async def test_validate_provider_key_rejects_401_without_leaking_secret(respx_mock):
    secret = "sk-ant-never-leak-this-secret-9999"
    respx_mock.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "invalid"})
    )

    result = await validate_provider_key(KeyType.ANTHROPIC, secret)

    assert result.ok is False
    assert result.status_code == 401
    assert secret not in result.detail


async def test_validate_provider_key_handles_network_error_without_leaking_secret(respx_mock):
    secret = "tmdb-secret-v3-key-7777"
    respx_mock.get("https://api.themoviedb.org/3/configuration").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    result = await validate_provider_key(KeyType.TMDB, secret)

    assert result.ok is False
    assert secret not in result.detail


async def test_validate_provider_key_empty_makes_no_network_call(respx_mock):
    result = await validate_provider_key(KeyType.OPENAI, "   ")
    assert result.ok is False
    assert len(respx_mock.calls) == 0
