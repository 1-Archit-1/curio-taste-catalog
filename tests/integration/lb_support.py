"""Shared helpers for the Letterboxd source and import integration tests."""

import io
import zipfile
from pathlib import Path
from uuid import UUID

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.clients.providers import KeyType
from curio.crypto import KeyEncryptor, Keypair
from curio.repositories import keys as keys_repo
from curio.settings import Settings

LB_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "letterboxd"
TMDB_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tmdb"
TMDB_BASE = "https://api.themoviedb.org/3"
FIXTURE_FILES = (
    "profile.csv",
    "watched.csv",
    "ratings.csv",
    "diary.csv",
    "watchlist.csv",
    "lists/sci-fi-favorites.csv",
)
RSS_URL = "https://letterboxd.com/alice/rss/"


def build_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def fixture_files(*, username: str | None = None, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """The anonymized fixture export as `{name: text}`, optionally claiming another username."""
    files = {
        rel: (LB_FIXTURES / rel).read_text(encoding="utf-8")
        for rel in FIXTURE_FILES
        if rel not in skip
    }
    if username is not None and "profile.csv" in files:
        files["profile.csv"] = files["profile.csv"].replace(",alice,", f",{username},")
    return files


def build_fixture_zip(*, username: str | None = None, skip: tuple[str, ...] = ()) -> bytes:
    return build_zip(fixture_files(username=username, skip=skip))  # type: ignore[arg-type]


def mock_tmdb_for_zip(respx_mock: respx.MockRouter) -> dict[str, respx.Route]:
    """Mock the TMDB routes the fixture export touches. Series search finds nothing."""
    movie_603 = (TMDB_FIXTURES / "movie_603.json").read_text(encoding="utf-8")
    movie_87101 = (TMDB_FIXTURES / "movie_87101.json").read_text(encoding="utf-8")

    def search_handler(request: httpx.Request) -> httpx.Response:
        query = (request.url.params.get("query") or "").lower()
        if "matrix" in query:
            results = [{"id": 603, "title": "The Matrix", "release_date": "1999-03-31"}]
        elif "terminator" in query:
            results = [{"id": 87101, "title": "Terminator Genisys", "release_date": "2015-07-01"}]
        elif "ambiguous" in query:
            results = [{"id": 603, "title": "Something Different", "release_date": "2020-01-01"}]
        else:
            results = []
        return httpx.Response(200, json={"results": results})

    return {
        "search_movie": respx_mock.get(f"{TMDB_BASE}/search/movie").mock(
            side_effect=search_handler
        ),
        "search_tv": respx_mock.get(f"{TMDB_BASE}/search/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        ),
        "movie_603": respx_mock.get(f"{TMDB_BASE}/movie/603").mock(
            return_value=httpx.Response(200, text=movie_603)
        ),
        "movie_87101": respx_mock.get(f"{TMDB_BASE}/movie/87101").mock(
            return_value=httpx.Response(200, text=movie_87101)
        ),
    }


def worker_settings(database_url: str, keypair: Keypair) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        key_public_key=keypair.public_key,
        key_private_key=keypair.private_key,
    )


async def save_committed_tmdb_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    keypair: Keypair,
    api_key: str = "tmdb-secret-key-1234",
) -> None:
    async with session_factory() as s:
        await keys_repo.upsert_api_key(
            s,
            user_id=user_id,
            key_type=KeyType.TMDB.value,
            encrypted_key=KeyEncryptor(keypair.public_key).encrypt(api_key),
            last4=api_key[-4:],
        )
        await s.commit()


async def login(client: httpx.AsyncClient, token: str) -> None:
    resp = await client.post("/api/auth/login", json={"token": token})
    assert resp.status_code == 200
