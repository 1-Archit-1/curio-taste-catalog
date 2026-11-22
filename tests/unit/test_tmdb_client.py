import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from curio.clients.ratelimit import RateLimiter
from curio.clients.tmdb import (
    BASE_URL,
    TmdbAuthError,
    TmdbClient,
    TmdbError,
    TmdbRateLimitError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tmdb"
V3_KEY = "v3secretkey0123456789abcdef0123"
V4_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0bWRiIn0.v4secretsignature"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def no_sleep(_: float) -> None:
    return None


def make_client(key: str = V3_KEY, **kwargs) -> TmdbClient:
    return TmdbClient(key, RateLimiter(1000, sleep=no_sleep), sleep=no_sleep, **kwargs)


async def test_search_by_title_and_year(respx_mock):
    route = respx_mock.get(f"{BASE_URL}/search/movie").mock(
        return_value=httpx.Response(200, json=fixture("search_movie.json"))
    )
    async with make_client() as client:
        refs = await client.search("movie", "The Matrix", 1999)
    assert [(r.tmdb_id, r.name, r.year) for r in refs] == [
        (603, "The Matrix", 1999),
        (684431, 'Making "The Matrix"', 1999),
        (684428, "The Matrix: What Is Bullet-Time?", 1999),
    ]
    params = route.calls.last.request.url.params
    assert params["query"] == "The Matrix"
    assert params["year"] == "1999"


async def test_tv_search_uses_first_air_date_year(respx_mock):
    route = respx_mock.get(f"{BASE_URL}/search/tv").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    async with make_client() as client:
        await client.search("series", "Breaking Bad", 2008)
    assert route.calls.last.request.url.params["first_air_date_year"] == "2008"


async def test_discover_and_recommendations(respx_mock):
    discover = respx_mock.get(f"{BASE_URL}/discover/movie").mock(
        return_value=httpx.Response(200, json=fixture("discover_movie.json"))
    )
    recs = respx_mock.get(f"{BASE_URL}/movie/603/recommendations").mock(
        return_value=httpx.Response(200, json=fixture("recommendations_movie_603.json"))
    )
    async with make_client() as client:
        found = await client.discover("movie", with_genres="27", sort_by="popularity.asc")
        recommended = await client.recommendations("movie", 603)
    params = discover.calls.last.request.url.params
    assert params["with_genres"] == "27"
    assert params["sort_by"] == "popularity.asc"
    assert params["page"] == "1"
    assert [(r.tmdb_id, r.name, r.year) for r in found] == [
        (1836957, "Mama Bear", 2026),
        (1836920, "Deadline", 2026),
    ]
    assert [r.media_type for r in found] == ["movie", "movie"]
    assert [(r.tmdb_id, r.name, r.year) for r in recommended] == [
        (87101, "Terminator Genisys", 2015),
        (777443, "The Electric State", 2025),
    ]
    assert recs.called


async def test_hydrate_movie_uses_one_request(respx_mock):
    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(
        return_value=httpx.Response(200, json=fixture("movie_603.json"))
    )
    async with make_client() as client:
        title = await client.hydrate("movie", 603)
    assert route.call_count == 1
    assert route.calls.last.request.url.params["append_to_response"] == (
        "external_ids,keywords,credits"
    )
    assert title.imdb_id == "tt0133093"
    assert (title.name, title.year, title.rating) == ("The Matrix", 1999, 8.3)
    assert title.poster_path == "/dXNAPwY7VrqMAo51EKhhCJfaGb5.jpg"
    assert title.genres == ["Action", "Science Fiction"]
    assert title.genre_ids == [28, 878]
    assert title.keywords[:3] == ["man vs machine", "martial arts", "fortune teller"]
    assert title.keyword_ids[:3] == [312, 779, 663]
    assert len(title.keywords) == len(title.keyword_ids) == 19
    assert title.cast == [
        "Keanu Reeves",
        "Laurence Fishburne",
        "Carrie-Anne Moss",
        "Hugo Weaving",
        "Gloria Foster",
        "Joe Pantoliano",
        "Marcus Chong",
        "Julian Arahanga",
        "Matt Doran",
        "Belinda McClory",
    ]
    assert title.directors == ["Lana Wachowski", "Lilly Wachowski"]
    assert title.vote_count == 29320
    assert title.popularity == 39.525
    assert title.original_language == "en"
    assert title.runtime == 136
    assert title.release_date == date(1999, 3, 31)


async def test_hydrate_series_reads_external_ids_and_creators(respx_mock):
    respx_mock.get(f"{BASE_URL}/tv/1396").mock(
        return_value=httpx.Response(200, json=fixture("tv_1396.json"))
    )
    async with make_client() as client:
        title = await client.hydrate("series", 1396)
    assert title.imdb_id == "tt0903747"
    assert (title.name, title.year) == ("Breaking Bad", 2008)
    assert title.genres == ["Drama", "Crime"]
    assert title.genre_ids == [18, 80]
    assert title.keywords[:3] == ["new mexico", "drug dealer", "narcissism"]
    assert title.keyword_ids[:3] == [1508, 2231, 5857]
    assert len(title.keywords) == len(title.keyword_ids) == 27
    assert title.directors == ["Vince Gilligan"]
    assert title.rating == 8.959
    assert title.vote_count == 19166
    assert title.popularity == 127.3355
    assert title.original_language == "en"
    assert title.release_date == date(2008, 1, 20)
    # The recorded response has an empty `episode_run_time`.
    assert title.runtime is None


@pytest.mark.parametrize(
    ("episode_run_time", "expected"), [([47, 60], 47), ([], None), ([0], None), (None, None)]
)
async def test_series_runtime_is_first_episode_run_time(respx_mock, episode_run_time, expected):
    data = fixture("tv_1396.json")
    data["episode_run_time"] = episode_run_time
    respx_mock.get(f"{BASE_URL}/tv/1396").mock(return_value=httpx.Response(200, json=data))
    async with make_client() as client:
        title = await client.hydrate("series", 1396)
    assert title.runtime == expected


@pytest.mark.parametrize(("runtime", "expected"), [(0, None), (None, None), (136, 136)])
async def test_movie_runtime_treats_zero_as_unknown(respx_mock, runtime, expected):
    data = fixture("movie_603.json")
    data["runtime"] = runtime
    respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(200, json=data))
    async with make_client() as client:
        title = await client.hydrate("movie", 603)
    assert title.runtime == expected


@pytest.mark.parametrize("value", ["", None, "not-a-date", "2026-13-45"])
async def test_empty_or_malformed_release_date_is_none(respx_mock, value):
    data = fixture("movie_603.json")
    data["release_date"] = value
    respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(200, json=data))
    async with make_client() as client:
        title = await client.hydrate("movie", 603)
    assert title.release_date is None


async def test_hydrate_title_without_imdb_id_and_unrated(respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/1836920").mock(
        return_value=httpx.Response(200, json=fixture("movie_1836920.json"))
    )
    async with make_client() as client:
        title = await client.hydrate("movie", 1836920)
    assert title.imdb_id is None
    assert title.name == "Deadline"
    assert title.rating is None
    assert title.vote_count == 0
    assert title.runtime == 6
    assert title.release_date == date(2026, 11, 20)
    assert (title.keywords, title.keyword_ids) == ([], [])
    assert title.genre_ids == [27, 18, 878]


async def test_hydrate_recorded_title_with_empty_release_date(respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/1836957").mock(
        return_value=httpx.Response(200, json=fixture("movie_1836957.json"))
    )
    async with make_client() as client:
        title = await client.hydrate("movie", 1836957)
    assert title.imdb_id == "tt27666891"
    assert (title.release_date, title.year) == (None, None)
    assert (title.runtime, title.popularity, title.vote_count) == (5, 0.0, 0)
    assert title.rating is None


async def test_hydrate_404_returns_none(respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/1").mock(return_value=httpx.Response(404))
    async with make_client() as client:
        assert await client.hydrate("movie", 1) is None


async def test_v3_key_goes_in_query_and_v4_token_in_bearer_header(respx_mock):
    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(
        return_value=httpx.Response(200, json=fixture("movie_603.json"))
    )
    async with make_client(V3_KEY) as client:
        await client.hydrate("movie", 603)
    request = route.calls.last.request
    assert request.url.params["api_key"] == V3_KEY
    assert "authorization" not in request.headers

    async with make_client(V4_TOKEN) as client:
        await client.hydrate("movie", 603)
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {V4_TOKEN}"
    assert "api_key" not in request.url.params


async def test_retries_429_honouring_retry_after(respx_mock):
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(429),
            httpx.Response(200, json=fixture("movie_603.json")),
        ]
    )
    client = TmdbClient(V3_KEY, RateLimiter(1000, sleep=no_sleep), sleep=record)
    async with client:
        title = await client.hydrate("movie", 603)
    assert title.tmdb_id == 603
    assert route.call_count == 3
    assert sleeps == [3.0, 2.0]  # Retry-After, then exponential backoff (2**1)


async def test_gives_up_after_max_retries(respx_mock):
    route = respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(429))
    async with make_client(max_retries=2) as client:
        with pytest.raises(TmdbRateLimitError):
            await client.hydrate("movie", 603)
    assert route.call_count == 3


async def test_auth_error(respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(401))
    async with make_client() as client:
        with pytest.raises(TmdbAuthError) as exc:
            await client.hydrate("movie", 603)
    assert V3_KEY not in str(exc.value)


async def test_rate_limiter_spaces_requests():
    now = 0.0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = RateLimiter(10, clock=lambda: now, sleep=fake_sleep)
    for _ in range(3):
        await limiter.acquire()
    assert sleeps == pytest.approx([0.1, 0.1])


@pytest.mark.parametrize("key", [V3_KEY, V4_TOKEN])
async def test_logs_from_tmdb_calls_contain_no_key(respx_mock, caplog, key):
    caplog.set_level(logging.DEBUG)
    respx_mock.get(f"{BASE_URL}/movie/603").mock(
        return_value=httpx.Response(200, json=fixture("movie_603.json"))
    )
    # Real httpx logs the full request URL at INFO.
    async with make_client(key) as client:
        await client.hydrate("movie", 603)
    # httpcore's DEBUG header dump, simulated: respx bypasses httpcore.
    logging.getLogger("httpx").debug("headers: %s", [("authorization", f"Bearer {key}")])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP Request: GET" in text  # httpx's own request line was logged
    assert key not in text
    assert "v4secretsignature" not in text


async def test_rate_limiter_back_off_delays_later_acquires():
    now = 0.0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = RateLimiter(10, clock=lambda: now, sleep=fake_sleep)
    await limiter.acquire()
    limiter.back_off(5)
    await limiter.acquire()
    assert sleeps == [5.0]


async def test_429_backs_off_the_shared_limiter(respx_mock):
    limiter = RateLimiter(1000)
    seen: list[float] = []
    limiter.back_off = seen.append  # type: ignore[method-assign]
    respx_mock.get(f"{BASE_URL}/movie/603").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "4"}),
            httpx.Response(200, json=fixture("movie_603.json")),
        ]
    )
    async with TmdbClient(V3_KEY, limiter, sleep=no_sleep) as client:
        await client.hydrate("movie", 603)
    assert seen == [4.0]


async def test_forbidden_reports_its_status(respx_mock):
    respx_mock.get(f"{BASE_URL}/movie/603").mock(return_value=httpx.Response(403))
    async with make_client() as client:
        with pytest.raises(TmdbAuthError) as exc:
            await client.hydrate("movie", 603)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json={"results": [{"title": "no id"}]}),
    ],
)
async def test_malformed_responses_raise_tmdb_error(respx_mock, response):
    respx_mock.get(f"{BASE_URL}/discover/movie").mock(return_value=response)
    async with make_client() as client:
        with pytest.raises(TmdbError):
            await client.discover("movie")


async def test_real_http_logs_contain_no_key(monkeypatch, caplog):
    """Real httpx + httpcore traffic against a local server, with DEBUG logging on."""
    import asyncio

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = json.dumps(fixture("movie_603.json")).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr("curio.clients.tmdb.BASE_URL", f"http://127.0.0.1:{port}/3")
    caplog.set_level(logging.DEBUG)
    try:
        for key in (V3_KEY, V4_TOKEN):
            async with make_client(key) as client:
                assert (await client.hydrate("movie", 603)).tmdb_id == 603
    finally:
        server.close()
        await server.wait_closed()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP Request: GET" in text
    assert V3_KEY not in text
    assert V4_TOKEN not in text
    assert "v4secretsignature" not in text
