"""Simkl client against shapes from the API docs (tests/fixtures/simkl, doc-derived)."""

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from curio.clients.simkl import (
    API_BASE_URL,
    SimklAuthError,
    SimklClient,
    SimklConfigError,
    SimklError,
    SimklRateLimitError,
    build_authorize_url,
    generate_pkce,
    parse_entries,
    parse_id_list,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "simkl"
ACCESS = "simkl_at_" + "A" * 34
REFRESH = "simkl_rt_" + "R" * 34


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_pkce_pair_is_s256_and_within_the_documented_length():
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert "=" not in challenge
    assert generate_pkce()[0] != verifier


def test_authorize_url_asks_for_read_scope_with_pkce_state_and_the_exact_redirect_uri():
    url = build_authorize_url(
        client_id="cid",
        redirect_uri="http://localhost:8000/api/simkl/callback",
        state="st",
        code_challenge="ch",
    )
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "simkl.com", "/oauth2/authorize")
    query = parse_qs(parts.query)
    assert {k: v[0] for k, v in query.items()} == {
        "client_id": "cid",
        "redirect_uri": "http://localhost:8000/api/simkl/callback",
        "response_type": "code",
        "scope": "media:read",
        "state": "st",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
    }


def test_shows_fixture_parses_statuses_ratings_and_skips_titles_without_tmdb():
    items = parse_entries(fixture("all_items_shows.json"))
    by_tmdb = {e.tmdb_id: e for e in items.entries}
    assert items.skipped_without_tmdb == 1
    assert {tmdb: e.status for tmdb, e in by_tmdb.items()} == {
        1396: "completed",
        1438: "dropped",
        70523: "watching",
        1981: "plantowatch",
        87108: "hold",
    }
    bb = by_tmdb[1396]
    # `user_rating`, 1-10 as Simkl sends it.
    assert (bb.media_type, bb.rating, bb.title, bb.year) == ("series", 10.0, "Breaking Bad", 2008)
    assert bb.url == "https://simkl.com/tv/2/breaking-bad"
    assert by_tmdb[70523].rating is None
    assert bb.last_watched_at is not None and bb.last_watched_at.year == 2025


def test_movies_and_anime_fixtures_map_media_types():
    movies = parse_entries(fixture("all_items_movies.json"))
    assert {e.tmdb_id: e.media_type for e in movies.entries} == {
        603: "movie",
        680: "movie",
        238: "movie",
        438631: "movie",
    }
    assert movies.entries[0].url == "https://simkl.com/movies/10/the-matrix"
    anime = parse_entries(fixture("all_items_anime.json"))
    assert anime.skipped_without_tmdb == 1
    types = {(e.tmdb_id, e.media_type) for e in anime.entries}
    assert (129, "movie") in types  # anime_type "movie"
    assert (30991, "series") in types
    assert [e.status for e in anime.entries if e.tmdb_id == 1429] == ["completed", "watching"]
    assert anime.entries[0].url == "https://simkl.com/anime/37089/cowboy-bebop"


def test_ratings_delta_keeps_removed_ratings_as_none():
    items = parse_entries(fixture("ratings_shows.json"))
    assert {e.tmdb_id: e.rating for e in items.entries} == {1396: 9.0, 1438: None}


@pytest.mark.parametrize("body", [{}, {"shows": None}, {"shows": [], "movies": []}])
def test_empty_libraries_parse_to_nothing(body):
    items = parse_entries(body)
    assert items.entries == [] and items.skipped_without_tmdb == 0


def test_unexpected_bodies_are_errors_not_empty_libraries():
    with pytest.raises(SimklError):
        parse_entries([])
    with pytest.raises(SimklError):
        parse_id_list("nope")


def test_legacy_notinteresting_is_dropped_and_bad_ratings_are_ignored():
    body = {
        "movies": [
            {
                "status": "notinteresting",
                "user_rating": 11,
                "movie": {"title": "X", "year": None, "ids": {"simkl": 1, "tmdb": 5}},
            },
            {
                "status": "completed",
                "user_rating": 0,
                "movie": {"title": "Y", "year": 2000, "ids": {"simkl": 2, "tmdb": "abc"}},
            },
        ]
    }
    items = parse_entries(body)
    assert [(e.tmdb_id, e.status, e.rating) for e in items.entries] == [(5, "dropped", None)]
    assert items.skipped_without_tmdb == 1


def test_id_list_counts_anime_as_either_media_type():
    titles = parse_id_list(fixture("ids_only_anime.json")).titles
    assert ("series", 30991) in titles and ("movie", 30991) in titles
    assert ("movie", 129) in titles
    shows = parse_id_list(fixture("ids_only_shows.json")).titles
    assert ("series", 1396) in shows and ("movie", 1396) not in shows
    assert ("movie", 603) in parse_id_list(fixture("ids_only_movies.json")).titles


def make_client(respx_mock: respx.MockRouter) -> SimklClient:
    return SimklClient("cid", "sec", http=httpx.AsyncClient(base_url=API_BASE_URL))


def assert_identified(request: httpx.Request, *, token: bool = True) -> None:
    params = request.url.params
    assert params["client_id"] == "cid"
    assert params["app-name"] == "curio"
    assert params["app-version"]
    assert request.headers["user-agent"].startswith("curio/")
    assert (request.headers.get("authorization") == f"Bearer {ACCESS}") is token


async def test_sync_requests_carry_identification_and_the_bearer_token(respx_mock):
    client = make_client(respx_mock)
    activities = respx_mock.get(f"{API_BASE_URL}/sync/activities").mock(
        return_value=httpx.Response(200, json=fixture("activities.json"))
    )
    all_items = respx_mock.get(f"{API_BASE_URL}/sync/all-items/shows").mock(
        return_value=httpx.Response(200, json=fixture("all_items_shows.json"))
    )
    settings = respx_mock.get(f"{API_BASE_URL}/users/settings").mock(
        return_value=httpx.Response(200, json=fixture("users_settings.json"))
    )
    assert (await client.get_activities(ACCESS))["all"] == "2025-06-01T09:00:00Z"
    assert len((await client.get_all_items(ACCESS, "shows")).entries) == 5
    account = await client.get_user_settings(ACCESS)
    assert (account.account_id, account.name) == (424242, "curio-fixture-user")
    for route in (activities, all_items, settings):
        assert_identified(route.calls.last.request)
    # The first full pull has no `date_from`.
    assert "date_from" not in all_items.calls.last.request.url.params


async def test_delta_passes_date_from_exactly_and_ratings_have_no_rating_segment(respx_mock):
    client = make_client(respx_mock)
    delta = respx_mock.get(f"{API_BASE_URL}/sync/all-items").mock(
        return_value=httpx.Response(200, json={})
    )
    ratings = respx_mock.get(f"{API_BASE_URL}/sync/ratings/movies").mock(
        return_value=httpx.Response(200, json=fixture("ratings_movies.json"))
    )
    ids = respx_mock.get(f"{API_BASE_URL}/sync/all-items/anime").mock(
        return_value=httpx.Response(200, json=fixture("ids_only_anime.json"))
    )
    stamp = "2026-05-08T14:23:11Z"
    assert (await client.get_all_items(ACCESS, None, date_from=stamp)).entries == []
    assert delta.calls.last.request.url.params["date_from"] == stamp
    assert (await client.get_ratings_delta(ACCESS, "movies", date_from=stamp)).entries[
        0
    ].rating == 10
    assert ratings.calls.last.request.url.params["date_from"] == stamp
    assert ratings.calls.last.request.url.path == "/sync/ratings/movies"
    await client.get_id_list(ACCESS, "anime")
    assert ids.calls.last.request.url.params["extended"] == "ids_only"
    for route in (delta, ratings, ids):
        assert_identified(route.calls.last.request)


async def test_code_exchange_uses_basic_auth_pkce_and_never_retries(respx_mock):
    token = respx_mock.post(f"{API_BASE_URL}/oauth2/token").mock(
        return_value=httpx.Response(200, json=fixture("oauth_token.json"))
    )
    async with make_client(respx_mock) as client:
        tokens = await client.exchange_code(
            code="the-code", redirect_uri="http://localhost/cb", code_verifier="v" * 50
        )
    request = token.calls.last.request
    form = parse_qs(request.content.decode())
    assert {k: v[0] for k, v in form.items()} == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "http://localhost/cb",
        "code_verifier": "v" * 50,
    }
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"cid:sec").decode()
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "sec" not in request.content.decode()  # the secret is in the header only
    assert tokens.can_read and tokens.expires_in == 604800
    assert "FIXTUREACCESS" not in repr(tokens)  # tokens stay out of reprs and logs


async def test_a_failed_code_exchange_is_one_request_and_a_safe_message(respx_mock):
    token = respx_mock.post(f"{API_BASE_URL}/oauth2/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "x"})
    )
    async with make_client(respx_mock) as client:
        with pytest.raises(SimklError) as exc_info:
            await client.exchange_code(code="c", redirect_uri="u", code_verifier="v")
    assert token.call_count == 1
    assert not isinstance(exc_info.value, SimklAuthError)
    assert "Connect again" in str(exc_info.value)


async def test_refresh_returns_the_same_refresh_token_and_maps_invalid_grant(respx_mock):
    route = respx_mock.post(f"{API_BASE_URL}/oauth2/token")
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "token_type": "Bearer",
                "expires_in": 604800,
                "refresh_token": REFRESH,
                "scope": "media:read",
            },
        )
    )
    async with make_client(respx_mock) as client:
        tokens = await client.refresh(REFRESH)
        assert tokens.refresh_token == REFRESH and tokens.access_token == "new-access"
        form = parse_qs(route.calls.last.request.content.decode())
        assert form == {"grant_type": ["refresh_token"], "refresh_token": [REFRESH]}
        route.mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        with pytest.raises(SimklAuthError) as exc_info:
            await client.refresh(REFRESH)
        assert REFRESH not in str(exc_info.value)
        route.mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
        with pytest.raises(SimklConfigError) as config_error:
            await client.refresh(REFRESH)
        assert not isinstance(config_error.value, SimklAuthError)
        assert config_error.value.permanent


async def test_revoke_posts_the_token_with_client_authentication(respx_mock):
    route = respx_mock.post(f"{API_BASE_URL}/oauth2/revoke").mock(return_value=httpx.Response(200))
    async with make_client(respx_mock) as client:
        await client.revoke(REFRESH)
    request = route.calls.last.request
    assert parse_qs(request.content.decode()) == {"token": [REFRESH]}
    assert request.headers["authorization"].startswith("Basic ")
    route.mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    async with make_client(respx_mock) as client:
        with pytest.raises(SimklConfigError):
            await client.revoke(REFRESH)


@pytest.mark.parametrize(
    ("response", "error_type", "permanent"),
    [
        (httpx.Response(401, json={"error": "invalid_token", "code": 401}), SimklAuthError, False),
        (httpx.Response(412, json={"error": "client_id_failed"}), SimklConfigError, True),
        (httpx.Response(403, json={"error": "insufficient_scope"}), SimklConfigError, True),
        (httpx.Response(400, json={"error": "max_items", "code": 400}), SimklConfigError, True),
        (httpx.Response(500), SimklError, False),
        (httpx.Response(200, text="not json"), SimklError, False),
    ],
)
async def test_api_errors_map_to_safe_exceptions(respx_mock, response, error_type, permanent):
    respx_mock.get(f"{API_BASE_URL}/sync/activities").mock(return_value=response)
    async with make_client(respx_mock) as client:
        with pytest.raises(error_type) as exc_info:
            await client.get_activities(ACCESS)
    assert exc_info.value.permanent is permanent
    assert ACCESS not in str(exc_info.value)


async def test_rate_limits_tell_the_per_second_cap_from_the_daily_quota(respx_mock):
    route = respx_mock.get(f"{API_BASE_URL}/sync/activities")
    async with make_client(respx_mock) as client:
        route.mock(return_value=httpx.Response(429, json={"error": "rate_limit", "code": 429}))
        with pytest.raises(SimklRateLimitError) as per_second:
            await client.get_activities(ACCESS)
        route.mock(
            return_value=httpx.Response(
                429, json={"error": "user_limit_exceeded"}, headers={"Retry-After": "45000"}
            )
        )
        with pytest.raises(SimklRateLimitError) as daily:
            await client.get_activities(ACCESS)
    assert (per_second.value.daily, per_second.value.permanent) == (False, False)
    assert (daily.value.daily, daily.value.permanent, daily.value.retry_after) == (
        True,
        True,
        45000,
    )


async def test_network_errors_do_not_leak_the_url(respx_mock):
    respx_mock.get(f"{API_BASE_URL}/sync/activities").mock(side_effect=httpx.ConnectError("boom"))
    async with make_client(respx_mock) as client:
        with pytest.raises(SimklError) as exc_info:
            await client.get_activities(ACCESS)
    assert "simkl.com" not in str(exc_info.value)
