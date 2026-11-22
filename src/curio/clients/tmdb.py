"""TMDB client: search, discover, recommendations, and one-call title hydration.

Rules (docs/architecture.md & ADR 0002): httpx only, never the database. The key is passed in
by the caller (obtained through `services.keys.get_tmdb_key`) and goes straight into the HTTP
client. It is never logged; see `clients.redaction`.
"""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from curio.clients.ratelimit import RateLimiter
from curio.clients.redaction import install_log_redaction

MediaType = Literal["movie", "series"]

BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_CAST = 10
_TMDB_PATH: dict[MediaType, str] = {"movie": "movie", "series": "tv"}


class TmdbError(Exception):
    """Raised for non-retryable TMDB failures. Messages never contain the key or URL."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TmdbAuthError(TmdbError):
    """TMDB rejected the key (401)."""


class TmdbRateLimitError(TmdbError):
    """Still rate limited (429) after exhausting retries."""


@dataclass(frozen=True)
class TmdbRef:
    """A title as returned by search, discover, and recommendations."""

    media_type: MediaType
    tmdb_id: int
    name: str | None = None
    year: int | None = None


@dataclass(frozen=True)
class TmdbTitle:
    """A fully described title. `imdb_id` is None when TMDB has none."""

    media_type: MediaType
    tmdb_id: int
    imdb_id: str | None
    name: str
    year: int | None
    description: str | None
    rating: float | None
    poster_path: str | None
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)


def _year(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _ref(media_type: MediaType, item: dict[str, Any]) -> TmdbRef:
    date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
    name = item.get("title") if media_type == "movie" else item.get("name")
    return TmdbRef(media_type, item["id"], name, _year(date))


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        limiter: RateLimiter,
        *,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        install_log_redaction()
        key = api_key.strip()
        self._headers = {"Accept": "application/json"}
        self._params: dict[str, str] = {}
        # v4 Read Access Tokens are JWTs; v3 keys are short hex strings (same rule as providers).
        if len(key) > 64 or "." in key:
            self._headers["Authorization"] = f"Bearer {key}"
        else:
            self._params["api_key"] = key
        self._limiter = limiter
        self._http = http or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self._owns_http = http is None
        self._max_retries = max_retries
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "TmdbClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """GET with rate limiting and 429 retry. Returns None on 404."""
        query = {**(params or {}), **self._params}
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            try:
                resp = await self._http.get(
                    f"{BASE_URL}{path}", params=query, headers=self._headers
                )
            except httpx.TimeoutException:
                # Omit exception text: it can carry the request URL.
                raise TmdbError("timed out contacting TMDB") from None
            except httpx.HTTPError:
                raise TmdbError("network error contacting TMDB") from None
            if resp.status_code == 429:
                if attempt == self._max_retries:
                    raise TmdbRateLimitError("TMDB rate limit persisted after retries", 429)
                delay = _retry_delay(resp, attempt)
                # Make every other in-flight request wait too, not just this one.
                self._limiter.back_off(delay)
                await self._sleep(delay)
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code in (401, 403):
                raise TmdbAuthError(
                    f"TMDB rejected the API key (HTTP {resp.status_code})", resp.status_code
                )
            if resp.status_code >= 400:
                raise TmdbError(f"TMDB returned HTTP {resp.status_code}", resp.status_code)
            try:
                return resp.json()
            except ValueError:
                raise TmdbError("TMDB returned a non-JSON response", resp.status_code) from None
        raise AssertionError("unreachable")  # pragma: no cover

    async def search(
        self, media_type: MediaType, title: str, year: int | None = None
    ) -> list[TmdbRef]:
        params: dict[str, Any] = {"query": title}
        if year is not None:
            params["year" if media_type == "movie" else "first_air_date_year"] = year
        data = await self._get(f"/search/{_TMDB_PATH[media_type]}", params)
        return _refs(media_type, data)

    async def discover(
        self, media_type: MediaType, *, page: int = 1, **filters: Any
    ) -> list[TmdbRef]:
        """`filters` are passed through as TMDB discover query parameters."""
        data = await self._get(
            f"/discover/{_TMDB_PATH[media_type]}", {**filters, "page": page}
        )
        return _refs(media_type, data)

    async def recommendations(
        self, media_type: MediaType, tmdb_id: int, *, page: int = 1
    ) -> list[TmdbRef]:
        data = await self._get(
            f"/{_TMDB_PATH[media_type]}/{tmdb_id}/recommendations", {"page": page}
        )
        return _refs(media_type, data)

    async def hydrate(self, media_type: MediaType, tmdb_id: int) -> TmdbTitle | None:
        """Details, external IDs, keywords, and credits in one request. None if TMDB 404s."""
        data = await self._get(
            f"/{_TMDB_PATH[media_type]}/{tmdb_id}",
            {"append_to_response": "external_ids,keywords,credits"},
        )
        if data is None:
            return None
        try:
            return _parse_title(media_type, data)
        except (KeyError, TypeError, AttributeError):
            raise TmdbError("TMDB returned an unexpected response shape") from None


def _refs(media_type: MediaType, data: dict[str, Any] | None) -> list[TmdbRef]:
    try:
        return [_ref(media_type, r) for r in (data or {}).get("results", [])]
    except (KeyError, TypeError, AttributeError):
        raise TmdbError("TMDB returned an unexpected response shape") from None


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    try:
        return max(0.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return float(2**attempt)


def _parse_title(media_type: MediaType, data: dict[str, Any]) -> TmdbTitle:
    ref = _ref(media_type, data)
    external = data.get("external_ids") or {}
    imdb_id = data.get("imdb_id") or external.get("imdb_id") or None
    kw = data.get("keywords") or {}
    keyword_items = kw.get("keywords" if media_type == "movie" else "results", [])
    credits_ = data.get("credits") or {}
    if media_type == "movie":
        directors = [c["name"] for c in credits_.get("crew", []) if c.get("job") == "Director"]
    else:
        directors = [c["name"] for c in data.get("created_by", [])]
    vote_count = data.get("vote_count")
    rating = data.get("vote_average") if vote_count else None
    return TmdbTitle(
        media_type=media_type,
        tmdb_id=ref.tmdb_id,
        imdb_id=imdb_id,
        name=ref.name or "",
        year=ref.year,
        description=data.get("overview") or None,
        rating=rating,
        poster_path=data.get("poster_path"),
        genres=[g["name"] for g in data.get("genres") or []],
        keywords=[k["name"] for k in keyword_items],
        cast=[c["name"] for c in credits_.get("cast", [])[:MAX_CAST]],
        directors=directors,
    )
