"""Shared helpers for the Simkl integration tests: fixtures, mocks, seeding and the worker."""

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import respx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.ratelimit import RateLimiter
from curio.crypto import KeyEncryptor, Keypair
from curio.db.models.catalogs import MediaItem
from curio.repositories import simkl as simkl_repo
from curio.repositories import sources as sources_repo
from curio.services import jobs as jobs_service
from curio.services import sources as sources_service
from curio.settings import Settings

SIMKL_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "simkl"
API = "https://api.simkl.com"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
BASE_URL = "https://curio.test"
REDIRECT_URI = f"{BASE_URL}/api/simkl/callback"
ACCESS = "simkl_at_ACCESSTOKENFORTESTS000000000000000000"
REFRESH = "simkl_rt_REFRESHTOKENFORTESTS00000000000000000"
NEW_ACCESS = "simkl_at_REFRESHEDACCESSTOKEN0000000000000000"
STAMP = "2025-06-01T09:00:00Z"
LATER = "2025-07-01T09:00:00Z"

# TMDB IDs of the fixtures, plus a range tests may use for their own titles.
FIXTURE_TMDB_IDS = (1396, 1438, 70523, 1981, 87108, 603, 680, 238, 438631, 30991, 129, 1429)
OWN_TMDB_IDS = range(900_001, 900_100)


def load(name: str) -> Any:
    return json.loads((SIMKL_FIXTURES / name).read_text(encoding="utf-8"))


def entry(
    key: str,
    title: str,
    tmdb: int | None,
    status: str,
    *,
    rating: int | None = None,
    watched: str | None = "2025-05-01T09:00:00Z",
    simkl_id: int | None = None,
    anime_type: str | None = None,
) -> dict[str, Any]:
    """One `/sync/all-items` row. `key` is `show` or `movie`."""
    ids: dict[str, Any] = {"simkl": simkl_id or (tmdb or 1) + 1_000_000, "slug": "x"}
    if tmdb is not None:
        ids["tmdb"] = str(tmdb)
    row: dict[str, Any] = {
        "added_to_watchlist_at": "2025-01-10T20:00:00Z",
        "last_watched_at": watched,
        "user_rated_at": "2025-05-01T10:00:00Z" if rating else None,
        "user_rating": rating,
        "status": status,
        key: {"title": title, "poster": None, "year": 2020, "ids": ids},
    }
    if anime_type:
        row["anime_type"] = anime_type
    return row


def activities(
    *,
    all_: str = STAMP,
    shows: dict[str, str | None] | None = None,
    movies: dict[str, str | None] | None = None,
    anime: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """An activities response. The buckets default to the fixture's, and `shows` etc. override."""
    body = copy.deepcopy(load("activities.json"))
    body["all"] = all_
    for key, over in (("tv_shows", shows), ("movies", movies), ("anime", anime)):
        body[key].update(over or {})
    return body


# --- Settings and app ------------------------------------------------------------------------


def configure_api(app: FastAPI, **overrides: Any) -> Settings:
    """Give the committed app a Simkl app registration. Pass `None` to unset a field."""
    values: dict[str, Any] = {
        "simkl_client_id": CLIENT_ID,
        "simkl_client_secret": SecretStr(CLIENT_SECRET),
        "public_base_url": BASE_URL,
        **overrides,
    }
    for name, value in values.items():
        setattr(app.state.settings, name, value)
    return app.state.settings  # type: ignore[no-any-return]


def worker_settings(database_url: str, keypair: Keypair, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "simkl_client_id": CLIENT_ID,
        "simkl_client_secret": CLIENT_SECRET,
        "public_base_url": BASE_URL,
        **overrides,
    }
    return Settings(
        _env_file=None,
        database_url=database_url,
        key_public_key=keypair.public_key,
        key_private_key=keypair.private_key,
        **values,
    )


async def run_worker(
    factory: async_sessionmaker[AsyncSession],
    database_url: str,
    keypair: Keypair,
    *,
    settings: Settings | None = None,
) -> bool:
    return await jobs_service.process_one_job(
        factory,
        settings=settings or worker_settings(database_url, keypair),
        rate_limiter=RateLimiter(1000),
        worker_id="simkl-test-worker",
    )


async def make_due(factory: async_sessionmaker[AsyncSession], job_id: Any) -> None:
    async with factory() as s:
        await s.execute(text("UPDATE jobs SET run_at = now() WHERE id = :id"), {"id": job_id})
        await s.commit()


# --- Seeding ---------------------------------------------------------------------------------


async def seed_connection(
    factory: async_sessionmaker[AsyncSession],
    keypair: Keypair,
    user_id: UUID,
    *,
    access: str = ACCESS,
    refresh: str = REFRESH,
    expires_in: timedelta = timedelta(days=5),
    snapshot: dict[str, Any] | None = None,
    account_name: str = "tester",
) -> UUID:
    """Store a connection and its source the way the callback does. Returns the source id."""
    encryptor = KeyEncryptor(keypair.public_key)
    async with factory() as s:
        source = await sources_repo.get_or_create_source(
            s,
            kind="simkl",
            owner_user_id=user_id,
            external_key="account",
            label=f"Simkl (@{account_name})",
            url="https://simkl.com/",
        )
        await sources_service.subscribe_user_to_source(
            s, user_id=user_id, source=source, role="self"
        )
        await simkl_repo.upsert_connection(
            s,
            user_id=user_id,
            source_id=source.id,
            access_token_ciphertext=encryptor.encrypt(access),
            refresh_token_ciphertext=encryptor.encrypt(refresh),
            access_expires_at=datetime.now(UTC) + expires_in,
            scope="media:read",
            simkl_account_id=424242,
            simkl_account_name=account_name,
        )
        if snapshot is not None:
            await simkl_repo.record_sync_success(s, user_id=user_id, snapshot=snapshot)
        await s.commit()
        return source.id


async def delete_test_titles(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as s:
        await s.execute(
            delete(MediaItem).where(
                MediaItem.tmdb_id.in_([*FIXTURE_TMDB_IDS, *OWN_TMDB_IDS]),
            )
        )
        await s.commit()


# --- Mocks -----------------------------------------------------------------------------------


class SimklMock:
    """The Simkl API on respx. Each attribute is a route; `set_*` swaps what it answers."""

    def __init__(self, respx_mock: respx.MockRouter) -> None:
        self.router = respx_mock
        self.activities = respx_mock.get(f"{API}/sync/activities").mock(
            return_value=httpx.Response(200, json=load("activities.json"))
        )
        self.all_items = {
            kind: respx_mock.get(f"{API}/sync/all-items/{kind}").mock(
                side_effect=self._all_items_handler(kind)
            )
            for kind in ("shows", "movies", "anime")
        }
        self.all_items_any = respx_mock.get(f"{API}/sync/all-items").mock(
            return_value=httpx.Response(200, json={})
        )
        self.ratings = {
            kind: respx_mock.get(f"{API}/sync/ratings/{kind}").mock(
                return_value=httpx.Response(200, json={})
            )
            for kind in ("shows", "movies", "anime")
        }
        self.token = respx_mock.post(f"{API}/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": NEW_ACCESS,
                    "token_type": "Bearer",
                    "expires_in": 604800,
                    "refresh_token": REFRESH,
                    "scope": "media:read",
                },
            )
        )
        self.revoke = respx_mock.post(f"{API}/oauth2/revoke").mock(return_value=httpx.Response(200))
        self.settings = respx_mock.get(f"{API}/users/settings").mock(
            return_value=httpx.Response(200, json=load("users_settings.json"))
        )
        # What a full (no `date_from`) and an `ids_only` request answer, per type.
        self.full: dict[str, Any] = {
            "shows": load("all_items_shows.json"),
            "movies": load("all_items_movies.json"),
            "anime": load("all_items_anime.json"),
        }
        self.ids: dict[str, Any] = {
            "shows": load("ids_only_shows.json"),
            "movies": load("ids_only_movies.json"),
            "anime": load("ids_only_anime.json"),
        }
        self.delta_by_type: dict[str, Any] = {}

    def _all_items_handler(self, kind: str) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            params = request.url.params
            if params.get("extended") == "ids_only":
                return httpx.Response(200, json=self.ids[kind])
            if "date_from" in params:
                return httpx.Response(200, json=self.delta_by_type.get(kind, {}))
            return httpx.Response(200, json=self.full[kind])

        return handler

    def set_activities(self, body: dict[str, Any]) -> None:
        self.activities.mock(return_value=httpx.Response(200, json=body))

    def requests(self, route: respx.Route) -> list[httpx.Request]:
        return [call.request for call in route.calls]

    def library_calls(self) -> list[str]:
        """The path and `date_from`/`extended` of every `/sync/all-items` request, in order."""
        out: list[str] = []
        for call in self.router.calls:
            url = call.request.url
            if url.path.startswith("/sync/all-items"):
                params = {k: v for k, v in url.params.items() if k in ("date_from", "extended")}
                out.append(url.path + (f"?{params}" if params else ""))
        return out


def all_requests_identified(mock: SimklMock) -> bool:
    for call in mock.router.calls:
        request = call.request
        if request.url.path.startswith("/oauth2/"):
            if not request.headers.get("user-agent", "").startswith("curio/"):
                return False
            continue
        params = request.url.params
        if params.get("client_id") != CLIENT_ID or params.get("app-name") != "curio":
            return False
        if not params.get("app-version") or not request.headers["user-agent"].startswith("curio/"):
            return False
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return False
    return True


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def assert_secrets_absent(factory: async_sessionmaker[AsyncSession], *secrets: str) -> None:
    """No plaintext token (or client secret) in any row of any table."""
    async with factory() as s:
        tables = [
            r[0]
            for r in await s.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
        ]
        for table in tables:
            rows = await s.execute(text(f'SELECT row_to_json(t)::text FROM "{table}" t'))
            dump = "\n".join(r[0] for r in rows)
            for secret in secrets:
                assert secret not in dump, f"{secret[:12]}... found in table {table}"
