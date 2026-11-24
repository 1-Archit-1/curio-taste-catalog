import json
import logging
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, update

from curio.clients.embeddings import BASE_URL as GEMINI_URL
from curio.clients.embeddings import EMBEDDING_DIMENSIONS, EmbeddingError, GeminiEmbedder
from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import BASE_URL, TmdbAuthError, TmdbClient, TmdbError, TmdbRef
from curio.db.models.catalogs import MediaItem
from curio.repositories import media as media_repo
from curio.services import media as media_service
from curio.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tmdb"
SETTINGS = Settings(_env_file=None, database_url="postgresql://unused")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def tmdb_routes(respx_mock):
    return {
        "movie_603": respx_mock.get(f"{BASE_URL}/movie/603").mock(
            return_value=httpx.Response(200, json=load("movie_603.json"))
        ),
        "movie_1836920": respx_mock.get(f"{BASE_URL}/movie/1836920").mock(
            return_value=httpx.Response(200, json=load("movie_1836920.json"))
        ),
        "movie_1836957": respx_mock.get(f"{BASE_URL}/movie/1836957").mock(
            return_value=httpx.Response(200, json=load("movie_1836957.json"))
        ),
        "movie_87101": respx_mock.get(f"{BASE_URL}/movie/87101").mock(
            return_value=httpx.Response(200, json=load("movie_87101.json"))
        ),
        "movie_777443": respx_mock.get(f"{BASE_URL}/movie/777443").mock(
            return_value=httpx.Response(200, json=load("movie_777443.json"))
        ),
        "movie_647763": respx_mock.get(f"{BASE_URL}/movie/647763").mock(
            return_value=httpx.Response(200, json=load("movie_647763.json"))
        ),
        "movie_4917": respx_mock.get(f"{BASE_URL}/movie/4917").mock(
            return_value=httpx.Response(200, json=load("movie_4917.json"))
        ),
        "tv_1396": respx_mock.get(f"{BASE_URL}/tv/1396").mock(
            return_value=httpx.Response(200, json=load("tv_1396.json"))
        ),
        "discover": respx_mock.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(200, json=load("discover_movie.json"))
        ),
    }


@pytest.fixture
async def tmdb():
    async with TmdbClient("v3secretkey0123456789abcdef0123", RateLimiter(1000)) as client:
        yield client


async def test_hydrate_stores_once_and_cache_hits_skip_tmdb(session, tmdb, tmdb_routes):
    refs = [TmdbRef("movie", 603), TmdbRef("movie", 603)]
    first = await media_service.hydrate_titles(session, tmdb, refs, settings=SETTINGS)
    second = await media_service.hydrate_titles(session, tmdb, refs, settings=SETTINGS)

    assert len(first) == len(second) == 1
    assert tmdb_routes["movie_603"].call_count == 1
    item = second[0]
    assert item.imdb_id == "tt0133093"
    assert item.poster_path == "/dXNAPwY7VrqMAo51EKhhCJfaGb5.jpg"
    assert item.poster_url == "https://image.tmdb.org/t/p/w500/dXNAPwY7VrqMAo51EKhhCJfaGb5.jpg"
    assert item.genres == ["Action", "Science Fiction"]
    assert item.keywords[:3] == ["man vs machine", "martial arts", "fortune teller"]
    assert item.directors == ["Lana Wachowski", "Lilly Wachowski"]
    assert item.hydrated_at is not None
    assert item.embedding is None


async def test_hydrate_persists_movie_and_series_metadata(session, tmdb, tmdb_routes):
    movie, series = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603), TmdbRef("series", 1396)], settings=SETTINGS
    )
    await session.refresh(movie)
    await session.refresh(series)
    assert movie.genre_ids == [28, 878]
    assert movie.keyword_ids[:3] == [312, 779, 663]
    assert len(movie.keyword_ids) == len(movie.keywords) == 19
    assert movie.vote_count == 29320
    assert movie.popularity == 39.525
    assert movie.original_language == "en"
    assert movie.runtime == 136
    assert movie.release_date == date(1999, 3, 31)
    assert (movie.collection_id, movie.collection_name) == (2344, "The Matrix Collection")
    assert movie.rating == 8.3
    assert series.genre_ids == [18, 80]
    assert series.keyword_ids[:3] == [1508, 2231, 5857]
    assert series.vote_count == 19166
    assert series.original_language == "en"
    assert series.runtime == 56  # empty episode_run_time falls back to last_episode_to_air
    assert series.release_date == date(2008, 1, 20)
    assert series.collection_id is None
    assert series.year == 2008


async def test_hydrate_stores_nulls_and_empty_arrays_for_sparse_title(session, tmdb, tmdb_routes):
    [item] = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 1836957)], settings=SETTINGS
    )
    await session.refresh(item)
    assert item.release_date is None
    assert item.year is None
    assert (item.genre_ids, item.keyword_ids) == ([], [])
    assert (item.vote_count, item.popularity, item.runtime) == (0, 0.0, 5)


async def test_second_upsert_refreshes_new_columns(session):
    fields = dict(
        media_type="movie",
        tmdb_id=603,
        imdb_id="tt0133093",
        name="The Matrix",
        year=1999,
        description=None,
        rating=8.3,
        poster_path=None,
        poster_url=None,
        genres=["Action"],
        keywords=["dystopia"],
        cast_names=[],
        directors=[],
    )
    await media_repo.upsert_hydrated(
        session,
        **fields,
        genre_ids=[28],
        keyword_ids=[4565],
        vote_count=100,
        popularity=1.5,
        original_language="en",
        runtime=120,
        release_date=date(1999, 3, 31),
        collection_id=2344,
        collection_name="The Matrix Collection",
    )
    item = await media_repo.upsert_hydrated(
        session,
        **fields,
        genre_ids=[28, 878],
        keyword_ids=[4565, 310],
        vote_count=250,
        popularity=2.5,
        original_language="fr",
        runtime=None,
        release_date=None,
        collection_id=None,
        collection_name=None,
    )
    await session.refresh(item)
    assert item.genre_ids == [28, 878]
    assert item.keyword_ids == [4565, 310]
    assert (item.vote_count, item.popularity, item.original_language) == (250, 2.5, "fr")
    assert (item.runtime, item.release_date) == (None, None)
    assert (item.collection_id, item.collection_name) == (None, None)


async def test_unhydrated_seed_row_is_hydrated_not_treated_as_cache_hit(session, tmdb, tmdb_routes):
    session.add(MediaItem(media_type="movie", tmdb_id=603, name="Seed placeholder"))
    await session.flush()
    [item] = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
    )
    assert item.name == "The Matrix"
    assert tmdb_routes["movie_603"].call_count == 1


async def test_discover_drops_titles_without_imdb_id_but_caches_them(session, tmdb, tmdb_routes):
    items = await media_service.discover_titles(session, tmdb, "movie", settings=SETTINGS)
    assert [i.tmdb_id for i in items] == [1836957]
    # The IMDb-less title is cached, so it is not hydrated again.
    await media_service.discover_titles(session, tmdb, "movie", settings=SETTINGS)
    assert tmdb_routes["movie_1836920"].call_count == 1


async def test_recommend_titles_hydrates_in_order(session, tmdb, tmdb_routes, respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/603/recommendations").mock(
        return_value=httpx.Response(200, json=load("recommendations_movie_603.json"))
    )
    items = await media_service.recommend_titles(session, tmdb, "movie", 603, settings=SETTINGS)
    assert [i.imdb_id for i in items] == ["tt1340138", "tt7766378"]


async def test_missing_title_is_skipped(session, tmdb, respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/1").mock(return_value=httpx.Response(404))
    items = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 1)], settings=SETTINGS
    )
    assert items == []


async def test_embed_missing_stores_normalized_vectors_once(session, tmdb, tmdb_routes, respx_mock):
    await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603), TmdbRef("movie", 1836920)], settings=SETTINGS
    )

    def reply(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["requests"])
        return httpx.Response(
            200, json={"embeddings": [{"values": [2.0] * EMBEDDING_DIMENSIONS}] * n}
        )

    gemini = respx_mock.post(
        f"{GEMINI_URL}/models/{SETTINGS.embedding_model}:batchEmbedContents"
    ).mock(side_effect=reply)
    embedder = GeminiEmbedder("gemini-key", SETTINGS.embedding_model)
    keys = [("movie", 603), ("movie", 1836920)]

    assert await media_service.embed_missing(session, embedder, keys) == 2
    # Second call finds nothing missing, so Gemini is not called again.
    assert await media_service.embed_missing(session, embedder, keys) == 0
    await embedder.aclose()
    assert gemini.call_count == 1

    rows = (
        await session.scalars(select(MediaItem).where(MediaItem.tmdb_id.in_([603, 1836920])))
    ).all()
    for row in rows:
        await session.refresh(row)
        assert len(row.embedding) == EMBEDDING_DIMENSIONS
        assert math.isclose(math.sqrt(sum(x * x for x in row.embedding)), 1.0, rel_tol=1e-6)


def test_embedding_text_includes_title_metadata():
    item = MediaItem(
        media_type="movie",
        tmdb_id=1,
        name="The Matrix",
        year=1999,
        description="A hacker.",
        genres=["Action", "Science Fiction"],
        keywords=["dystopia"],
        directors=["Lana Wachowski"],
        cast_names=["Keanu Reeves"],
    )
    text = media_service.embedding_text(item)
    assert text.splitlines() == [
        "The Matrix (1999)",
        "Genres: Action, Science Fiction",
        "Keywords: dystopia",
        "Director: Lana Wachowski",
        "A hacker.",
    ]
    assert "Keanu Reeves" not in text and "Cast" not in text


def test_per_process_rate_limit_divides_total_budget():
    settings = Settings(
        _env_file=None,
        database_url="postgresql://unused",
        tmdb_requests_per_second_total=40,
        worker_process_count=4,
    )
    assert settings.tmdb_requests_per_second_per_process == 10


async def test_clients_get_keys_only_through_key_access_function(
    session, audit_session_factory, make_committed_user, keypair, respx_mock
):
    from curio.clients.providers import KeyType
    from curio.crypto import KeyEncryptor
    from curio.repositories import keys as keys_repo
    from curio.services import keys as keys_service

    user = (await make_committed_user("media-keys-user")).user
    encryptor = KeyEncryptor(keypair.public_key)
    secrets = ((KeyType.TMDB, "tmdb-secret-0123456789abcdef"), (KeyType.GEMINI, "gem-secret"))
    for key_type, secret in secrets:
        await keys_repo.upsert_api_key(
            session,
            user_id=user.id,
            key_type=key_type.value,
            encrypted_key=encryptor.encrypt(secret),
            last4=secret[-4:],
        )
    settings = Settings(
        _env_file=None, database_url="postgresql://unused", key_private_key=keypair.private_key
    )
    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(
        return_value=httpx.Response(200, json=load("movie_603.json"))
    )
    client = await media_service.tmdb_client_for_user(
        session,
        audit_session_factory=audit_session_factory,
        user_id=user.id,
        job_id="job-1",
        settings=settings,
        limiter=RateLimiter(1000),
    )
    async with client:
        await client.hydrate("movie", 603)
    assert route.calls.last.request.url.params["api_key"] == "tmdb-secret-0123456789abcdef"

    embedder = await media_service.embedder_for_user(
        session,
        audit_session_factory=audit_session_factory,
        user_id=user.id,
        job_id="job-1",
        settings=settings,
    )
    await embedder.aclose()

    entries = await keys_service.list_user_audit_entries(session, user_id=user.id)
    assert {e.key_type for e in entries} == {"tmdb", "gemini"}


async def test_imdb_id_collision_keeps_metadata_without_poisoning_transaction(
    session, tmdb, tmdb_routes, caplog
):
    # A different title already owns tt0133093 (e.g. a seeded row).
    session.add(MediaItem(media_type="movie", tmdb_id=1, imdb_id="tt0133093", name="Other"))
    await session.flush()
    items = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603), TmdbRef("movie", 1836920)], settings=SETTINGS
    )
    by_id = {i.tmdb_id: i for i in items}
    assert by_id[603].imdb_id is None
    assert by_id[603].name == "The Matrix"
    assert by_id[1836920].name == "Deadline"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("movie 603" in m and "tt0133093" in m for m in warnings)
    # The session is still usable.
    assert (await session.get(MediaItem, ("movie", 1))).imdb_id == "tt0133093"


async def test_partial_failure_stores_successes_then_raises(session, tmdb, respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/603").mock(
        return_value=httpx.Response(200, json=load("movie_603.json"))
    )
    respx_mock.get(f"{BASE_URL}/movie/500").mock(return_value=httpx.Response(500))
    with pytest.raises(TmdbError):
        await media_service.hydrate_titles(
            session, tmdb, [TmdbRef("movie", 500), TmdbRef("movie", 603)], settings=SETTINGS
        )
    stored = await session.get(MediaItem, ("movie", 603))
    assert stored is not None and stored.hydrated_at is not None


async def test_embeddings_stored_per_batch_and_never_overwritten(
    session, tmdb, tmdb_routes, respx_mock
):
    await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603), TmdbRef("movie", 1836920)], settings=SETTINGS
    )
    ok = httpx.Response(200, json={"embeddings": [{"values": [1.0] * EMBEDDING_DIMENSIONS}]})
    respx_mock.post(f"{GEMINI_URL}/models/{SETTINGS.embedding_model}:batchEmbedContents").mock(
        side_effect=[ok, httpx.Response(400)]
    )
    keys = [("movie", 603), ("movie", 1836920)]
    async with GeminiEmbedder("k", SETTINGS.embedding_model, batch_size=1) as embedder:
        with pytest.raises(EmbeddingError):
            await media_service.embed_missing(session, embedder, keys)
    first = await session.get(MediaItem, ("movie", 603))
    second = await session.get(MediaItem, ("movie", 1836920))
    await session.refresh(first)
    await session.refresh(second)
    assert first.embedding is not None  # the first batch survived the second batch's failure
    assert second.embedding is None

    # An embedding that already exists is not overwritten.
    await media_repo.set_embedding(
        session, media_type="movie", tmdb_id=603, embedding=[0.5] * EMBEDDING_DIMENSIONS
    )
    await session.refresh(first)
    assert first.embedding[0] != 0.5


async def test_auth_error_stops_remaining_fetches_but_keeps_earlier_titles(
    session, tmdb, respx_mock
):
    refs = [TmdbRef("movie", i) for i in range(1000, 1040)]
    # Hydrate one title first; it must still be stored after the later auth failure.
    respx_mock.get(f"{BASE_URL}/movie/603").mock(
        return_value=httpx.Response(200, json=load("movie_603.json"))
    )
    await media_service.hydrate_titles(session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS)
    unauthorized = respx_mock.get(url__regex=rf"{BASE_URL}/movie/10\d\d").mock(
        return_value=httpx.Response(401)
    )
    with pytest.raises(TmdbAuthError):
        await media_service.hydrate_titles(session, tmdb, refs, settings=SETTINGS)
    assert unauthorized.call_count < len(refs)
    assert unauthorized.call_count <= media_service.HYDRATE_CONCURRENCY
    assert (await session.get(MediaItem, ("movie", 603))).hydrated_at is not None


async def test_stale_cache_entries_rehydrate_on_demand_only(session, tmdb, tmdb_routes):
    now = datetime.now(UTC)
    stale_time = now - timedelta(days=8)
    fresh_time = now - timedelta(days=2)

    # 603 is stale and will reappear; 87101 is stale and will NOT reappear; 777443 is fresh.
    session.add_all(
        [
            MediaItem(
                media_type="movie",
                tmdb_id=603,
                imdb_id="tt0133093",
                name="Old Matrix",
                hydrated_at=stale_time,
            ),
            MediaItem(
                media_type="movie",
                tmdb_id=87101,
                imdb_id="tt1340138",
                name="Old Terminator",
                hydrated_at=stale_time,
            ),
            MediaItem(
                media_type="movie",
                tmdb_id=777443,
                imdb_id="tt7766378",
                name="Fresh Electric State",
                hydrated_at=fresh_time,
            ),
        ]
    )
    await session.flush()

    items = await media_service.hydrate_titles(
        session,
        tmdb,
        [TmdbRef("movie", 603), TmdbRef("movie", 777443)],
        settings=SETTINGS,
    )
    by_id = {i.tmdb_id: i for i in items}
    assert by_id[603].name == "The Matrix"
    assert by_id[603].hydrated_at > stale_time
    assert by_id[777443].name == "Fresh Electric State"

    assert tmdb_routes["movie_603"].call_count == 1
    assert tmdb_routes["movie_777443"].call_count == 0
    assert tmdb_routes["movie_87101"].call_count == 0

    untouched = await session.get(MediaItem, ("movie", 87101))
    assert untouched.name == "Old Terminator"
    assert untouched.hydrated_at == stale_time


async def test_similar_titles_hydrates_in_order(session, tmdb, tmdb_routes, respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/603/similar").mock(
        return_value=httpx.Response(200, json=load("similar_movie_603.json"))
    )
    items = await media_service.similar_titles(session, tmdb, "movie", 603, settings=SETTINGS)
    # Both titles are hydrated in order; 647763 has no IMDb ID, so it can't be served.
    assert [i.tmdb_id for i in items] == [4917]
    assert items[0].imdb_id == "tt0072730"
    assert tmdb_routes["movie_647763"].call_count == 1


async def test_discover_unexcluded_walks_pages_skips_excluded_and_stops_at_limit(
    session, tmdb, tmdb_routes, respx_mock
):
    page1 = {
        "page": 1,
        "results": [
            {"id": 603, "title": "The Matrix", "release_date": "1999-03-31"},
            {"id": 1836920, "title": "Deadline", "release_date": "2026-11-20"},  # no imdb_id
            {"id": 1836957, "title": "Mama Bear", "release_date": ""},
        ],
    }
    page2 = {
        "page": 2,
        "results": [
            {"id": 87101, "title": "Terminator Genisys", "release_date": "2015-06-23"},
            {"id": 777443, "title": "The Electric State", "release_date": "2025-02-24"},
        ],
    }
    discover_route = respx_mock.get(f"{BASE_URL}/discover/movie").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
            httpx.Response(200, json={"page": 3, "results": []}),
        ]
    )

    items = await media_service.discover_unexcluded_titles(
        session,
        tmdb,
        "movie",
        limit=2,
        excluded_keys={("movie", 603)},
        settings=SETTINGS,
    )
    assert [i.tmdb_id for i in items] == [1836957, 87101]
    assert discover_route.call_count == 2
    assert tmdb_routes["movie_603"].call_count == 0  # excluded before hydration
    assert tmdb_routes["movie_777443"].call_count == 1  # hydrated on page 2, limit sliced to 2


async def test_page_walking_stops_at_max_pages_or_empty_page(
    session, tmdb, tmdb_routes, respx_mock
):
    settings = Settings(_env_file=None, database_url="postgresql://unused", tmdb_max_pages=2)
    page1 = {"page": 1, "results": [{"id": 603, "title": "The Matrix", "release_date": "1999"}]}
    page2 = {"page": 2, "results": [{"id": 87101, "title": "Terminator", "release_date": "2015"}]}
    rec_route = respx_mock.get(f"{BASE_URL}/movie/603/recommendations").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
            httpx.Response(200, json={"page": 3, "results": []}),
        ]
    )
    items = await media_service.recommend_unexcluded_titles(
        session, tmdb, "movie", 603, limit=10, settings=settings
    )
    assert [i.tmdb_id for i in items] == [603, 87101]
    assert rec_route.call_count == 2  # stopped at tmdb_max_pages=2

    sim_route = respx_mock.get(f"{BASE_URL}/movie/603/similar").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json={"page": 2, "results": []}),
        ]
    )
    sim_items = await media_service.similar_unexcluded_titles(
        session, tmdb, "movie", 603, limit=10, settings=SETTINGS
    )
    assert [i.tmdb_id for i in sim_items] == [603]
    assert sim_route.call_count == 2  # stopped on empty page 2 before tmdb_max_pages=5


def _age(days: float, seconds: float = 0):
    """SQL expression for `now() - days`: the database clock, as the cache uses."""
    return func.now() - timedelta(days=days, seconds=seconds)


async def _seed_cached(session, tmdb_id: int, name: str, *, age_days: float, seconds: float = 0):
    session.add(
        MediaItem(
            media_type="movie",
            tmdb_id=tmdb_id,
            imdb_id=f"tt{tmdb_id:07d}",
            name=name,
            hydrated_at=_age(age_days, seconds),
        )
    )
    await session.flush()


async def _db_now(session) -> datetime:
    return (await session.execute(select(func.now()))).scalar_one()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize(
    "route_kwargs",
    [
        {"return_value": httpx.Response(500)},
        {"return_value": httpx.Response(429, headers={"Retry-After": "0"})},
        {"side_effect": httpx.ReadTimeout("slow")},
    ],
    ids=["5xx", "429", "timeout"],
)
async def test_stale_refresh_failure_serves_the_cached_row(
    session, tmdb, respx_mock, caplog, route_kwargs
):
    await _seed_cached(session, 603, "Old Matrix", age_days=8)
    stale_at = (await session.get(MediaItem, ("movie", 603))).hydrated_at
    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(**route_kwargs)
    items = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
    )
    assert [i.name for i in items] == ["Old Matrix"]
    assert route.called
    # Still stale, so the next request tries again.
    assert items[0].hydrated_at == stale_at
    assert any("movie 603" in m and "serving the cached row" in m for m in _warnings(caplog))


async def test_entirely_stale_batch_swallows_auth_error(session, tmdb, respx_mock, caplog):
    await _seed_cached(session, 603, "Old Matrix", age_days=8)
    await _seed_cached(session, 87101, "Old Terminator", age_days=9)
    respx_mock.get(url__regex=rf"{BASE_URL}/movie/\d+").mock(return_value=httpx.Response(401))
    items = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603), TmdbRef("movie", 87101)], settings=SETTINGS
    )
    assert [i.name for i in items] == ["Old Matrix", "Old Terminator"]
    assert any("TmdbAuthError" in m for m in _warnings(caplog))


async def test_auth_error_for_a_never_hydrated_title_still_raises(session, tmdb, respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(401))
    with pytest.raises(TmdbAuthError):
        await media_service.hydrate_titles(
            session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
        )


async def test_stale_auth_error_raises_when_a_never_hydrated_title_failed(
    session, tmdb, respx_mock
):
    await _seed_cached(session, 603, "Old Matrix", age_days=8)
    respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(401))
    respx_mock.get(f"{BASE_URL}/movie/87101").mock(return_value=httpx.Response(500))
    with pytest.raises(TmdbAuthError):
        await media_service.hydrate_titles(
            session, tmdb, [TmdbRef("movie", 87101), TmdbRef("movie", 603)], settings=SETTINGS
        )


async def test_cache_age_boundary_is_exclusive(session, tmdb, tmdb_routes):
    max_age = SETTINGS.tmdb_cache_max_age_days
    await _seed_cached(session, 603, "Exactly Max Age", age_days=max_age)
    await _seed_cached(session, 87101, "A Second Too Old", age_days=max_age, seconds=1)
    items = await media_service.hydrate_titles(
        session,
        tmdb,
        [TmdbRef("movie", 603), TmdbRef("movie", 87101)],
        settings=SETTINGS,
    )
    assert {i.tmdb_id: i.name for i in items} == {
        603: "Exactly Max Age",
        87101: "Terminator Genisys",
    }
    assert tmdb_routes["movie_603"].call_count == 0
    assert tmdb_routes["movie_87101"].call_count == 1


async def test_stale_title_that_now_404s_keeps_cached_row_and_is_not_refetched(
    session, tmdb, respx_mock
):
    await _seed_cached(session, 603, "Old Matrix", age_days=8)
    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(404))
    first = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
    )
    assert [i.name for i in first] == ["Old Matrix"]
    assert first[0].hydrated_at == await _db_now(session)
    again = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
    )
    assert [i.name for i in again] == ["Old Matrix"]
    assert route.call_count == 1


_EMBEDDING_FIELDS = dict(
    media_type="movie",
    tmdb_id=603,
    imdb_id="tt0133093",
    name="The Matrix",
    year=1999,
    description="A hacker.",
    rating=8.3,
    poster_path=None,
    poster_url=None,
    genres=["Action"],
    keywords=["dystopia"],
    cast_names=["Keanu Reeves"],
    directors=["Lana Wachowski"],
    genre_ids=[28],
    keyword_ids=[4565],
    vote_count=100,
    popularity=1.5,
    original_language="en",
    runtime=120,
    release_date=date(1999, 3, 31),
    collection_id=None,
    collection_name=None,
)
_CHANGED = {
    "name": "The Matrix Reloaded",
    "year": 2003,
    "description": "A hacker, again.",
    "genres": ["Action", "Science Fiction"],
    "keywords": ["dystopia", "simulation"],
    "directors": ["Lana Wachowski", "Lilly Wachowski"],
}


async def _stored_embedding(session, **overrides):
    await media_repo.upsert_hydrated(session, **_EMBEDDING_FIELDS)
    await media_repo.set_embedding(
        session, media_type="movie", tmdb_id=603, embedding=[0.5] * EMBEDDING_DIMENSIONS
    )
    item = await media_repo.upsert_hydrated(session, **{**_EMBEDDING_FIELDS, **overrides})
    await session.refresh(item)
    return item


async def test_refresh_with_unchanged_embedding_inputs_keeps_the_vector(session):
    item = await _stored_embedding(
        session,
        # Fields the embedding does not use may change.
        rating=7.0,
        vote_count=500,
        cast_names=["Someone Else"],
        genre_ids=[28, 878],
        keyword_ids=[4565, 1],
    )
    assert item.embedding is not None and item.embedding[0] == 0.5
    assert item.vote_count == 500


@pytest.mark.parametrize("column", sorted(_CHANGED))
async def test_refresh_clears_the_vector_when_an_embedding_input_changed(session, column):
    item = await _stored_embedding(session, **{column: _CHANGED[column]})
    assert item.embedding is None
    assert getattr(item, column) == _CHANGED[column]


async def test_refresh_clears_the_vector_when_an_input_becomes_null(session):
    item = await _stored_embedding(session, description=None)
    assert item.embedding is None


def test_embedding_input_columns_match_embedding_text():
    def make(**overrides):
        fields = {
            "name": "N",
            "year": 2000,
            "description": "D",
            "genres": ["G"],
            "keywords": ["K"],
            "directors": ["R"],
            "cast_names": ["C"],
        }
        return MediaItem(media_type="movie", tmdb_id=1, **{**fields, **overrides})

    base_text = media_service.embedding_text(make())
    assert set(_CHANGED) == set(media_repo.EMBEDDING_INPUT_COLUMNS)
    for column, value in _CHANGED.items():
        assert media_service.embedding_text(make(**{column: value})) != base_text, column
    assert media_service.embedding_text(make(cast_names=["Other"])) == base_text


async def test_stale_refresh_keeps_embedding_when_tmdb_data_is_unchanged(session, tmdb, respx_mock):
    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(
        return_value=httpx.Response(200, json=load("movie_603.json"))
    )
    [item] = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
    )
    await media_repo.set_embedding(
        session, media_type="movie", tmdb_id=603, embedding=[0.25] * EMBEDDING_DIMENSIONS
    )
    await session.execute(update(MediaItem).values(hydrated_at=_age(8)))
    [refreshed] = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
    )
    await session.refresh(refreshed)
    assert route.call_count == 2
    assert refreshed.embedding is not None and refreshed.embedding[0] == 0.25
    # TMDB now reports different keywords: the vector is dropped so it is re-embedded.
    changed = load("movie_603.json")
    changed["keywords"]["keywords"] = changed["keywords"]["keywords"][:2]
    route.mock(return_value=httpx.Response(200, json=changed))
    await session.execute(update(MediaItem).values(hydrated_at=_age(8)))
    [refreshed] = await media_service.hydrate_titles(
        session, tmdb, [TmdbRef("movie", 603)], settings=SETTINGS
    )
    await session.refresh(refreshed)
    assert refreshed.embedding is None
    assert len(refreshed.keywords) == 2
