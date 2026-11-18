import httpx
import psycopg

from curio import ADDON_NAME
from curio.api.app import create_app
from curio.api.deps import get_conn
from curio.settings import Settings

SETTINGS = Settings(_env_file=None, database_url="postgresql://unused@127.0.0.1:1/unused")


def test_factory_builds_independent_apps_without_opening_a_pool():
    a, b = create_app(SETTINGS), create_app(SETTINGS)
    assert a is not b
    assert a.title == ADDON_NAME
    # The pool is created in the lifespan, not at construction or import time.
    assert not hasattr(a.state, "pool")


async def test_dependency_override_replaces_db_connection():
    app = create_app(SETTINGS)

    async def unavailable():
        raise psycopg.OperationalError("db down")
        yield  # pragma: no cover

    app.dependency_overrides[get_conn] = unavailable
    # ASGITransport does not run the lifespan, so the real pool is never opened.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "error"}
