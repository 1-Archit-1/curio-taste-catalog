import httpx
import psycopg
import pytest

from curio import ADDON_NAME
from curio.api.app import create_app
from curio.api.deps import get_session
from curio.settings import Settings

SETTINGS = Settings(_env_file=None, database_url="postgresql://unused@127.0.0.1:1/unused")


def test_factory_builds_independent_apps_without_opening_an_engine():
    a, b = create_app(SETTINGS), create_app(SETTINGS)
    assert a is not b
    assert a.title == ADDON_NAME
    # The engine and session factory are created in the lifespan, not at construction time.
    assert not hasattr(a.state, "engine")
    assert not hasattr(a.state, "session_factory")


def test_factory_refuses_to_build_with_the_private_key():
    settings = Settings(
        _env_file=None,
        database_url="postgresql://unused@127.0.0.1:1/unused",
        key_private_key="some-private-key",
    )
    with pytest.raises(RuntimeError, match="KEY_PRIVATE_KEY"):
        create_app(settings)


async def test_dependency_override_replaces_db_session():
    app = create_app(SETTINGS)

    async def unavailable():
        raise psycopg.OperationalError("db down")
        yield  # pragma: no cover

    app.dependency_overrides[get_session] = unavailable
    # ASGITransport does not run the lifespan, so the real engine is never opened.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "error"}
