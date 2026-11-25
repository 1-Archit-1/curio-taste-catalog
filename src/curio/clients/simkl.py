"""Simkl client: AUTH V2 (authorization code + PKCE, refresh, revoke) and the read-side Sync API.

Rules (docs/architecture.md & ADR 0002): httpx only, never the database. Tokens and the client
secret are passed in by the caller and go straight into request headers. They are never logged
and never appear in an error message (the messages here are shown to users).

Every request carries the identification Simkl requires: `client_id`, `app-name` and
`app-version` as URL parameters, and a `User-Agent` header
(https://api.simkl.org/conventions/headers). The `/oauth2/*` calls take only the `User-Agent`
(https://api.simkl.org/api-reference/oauth2-tokens).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx

from curio import __version__
from curio.clients.redaction import install_log_redaction

MediaType = Literal["movie", "series"]
# The `{type}` path segment of the Sync API. Activities call the same buckets `tv_shows`.
SimklType = Literal["shows", "movies", "anime"]

# The issuer in the `iss` parameter of the authorization response, and the consent page's host.
# https://api.simkl.org/api-reference/oauth2-authorization-code
SIMKL_ISSUER = "https://simkl.com"
AUTHORIZE_URL = f"{SIMKL_ISSUER}/oauth2/authorize"
API_BASE_URL = "https://api.simkl.com"
# Read access is all the sync needs. `media:write` would also let the app change the library.
# https://api.simkl.org/api-reference/oauth2-scopes
SCOPE = "media:read"
APP_NAME = "curio"
APP_VERSION = __version__
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
DEFAULT_TIMEOUT_SECONDS = 30.0
# TMDB IDs are stored in a 32-bit integer column.
_MAX_TMDB_ID = 2_147_483_647
SIMKL_TYPES: tuple[SimklType, ...] = ("shows", "movies", "anime")


class SimklError(Exception):
    """A Simkl failure. Messages are safe to show to the user and never hold tokens.

    `permanent` marks a failure that retrying the job cannot fix.
    """

    permanent = False

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SimklAuthError(SimklError):
    """Simkl no longer accepts the user's grant (rejected access token or `invalid_grant`).

    The user has to connect again.
    """


class SimklRateLimitError(SimklError):
    """429. `daily` is the per-user daily quota, which no retry within the day can clear."""

    def __init__(self, message: str, *, daily: bool, retry_after: int | None = None) -> None:
        super().__init__(message, status_code=429)
        self.daily = daily
        self.permanent = daily
        self.retry_after = retry_after


class SimklConfigError(SimklError):
    """Simkl rejected this server's credentials or the request itself. Needs an operator."""

    permanent = True


@dataclass(frozen=True)
class SimklTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    scope: str

    @property
    def can_read(self) -> bool:
        # `media:write` implies `media:read`.
        granted = set(self.scope.split())
        return "media:read" in granted or "media:write" in granted


@dataclass(frozen=True)
class SimklAccount:
    account_id: int | None
    name: str | None


@dataclass(frozen=True)
class SimklEntry:
    """One library entry, reduced to what Curio keeps."""

    media_type: MediaType
    tmdb_id: int
    # Simkl's own status: watching, plantowatch, hold, completed or dropped.
    status: str
    title: str
    year: int | None
    # The user's own rating, 1-10 as Simkl sends it (`user_rating`), or `None` when unrated.
    rating: float | None
    last_watched_at: datetime | None
    url: str | None


@dataclass(frozen=True)
class SimklItems:
    """Parsed entries plus how many Simkl sent without a usable TMDB ID."""

    entries: list[SimklEntry] = field(default_factory=list)
    skipped_without_tmdb: int = 0


@dataclass(frozen=True)
class SimklIdList:
    """Every title the user holds, as `(media_type, tmdb_id)`, for the removal diff."""

    titles: frozenset[tuple[MediaType, int]] = frozenset()


def generate_pkce() -> tuple[str, str]:
    """Return `(code_verifier, code_challenge)` for the S256 method (RFC 7636).

    https://api.simkl.org/api-reference/oauth2-pkce: 43-128 characters from the unreserved set;
    the challenge is the unpadded base64url SHA-256 of the verifier.
    """
    verifier = secrets.token_urlsafe(48)  # 64 characters of A-Z a-z 0-9 - _
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorize_url(
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    """The consent page URL. It is on simkl.com, not api.simkl.com, and the browser opens it."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        quote_via=quote,
    )
    return f"{AUTHORIZE_URL}?{query}"


def parse_simkl_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _tmdb_id(ids: object) -> int | None:
    # Every ID but `simkl` is a string in responses (standard-media-objects).
    raw = ids.get("tmdb") if isinstance(ids, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return None
    text = str(raw).strip()
    if not text.isdecimal():
        return None
    value = int(text)
    return value if 1 <= value <= _MAX_TMDB_ID else None


def _rating(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if 1 <= value <= 10 else None


def _simkl_url(section: str, ids: dict[str, Any]) -> str | None:
    # API rules: link back to the item's own Simkl page, with the slug when there is one.
    # https://api.simkl.org/api-rules
    simkl_id = ids.get("simkl")
    if isinstance(simkl_id, bool) or not isinstance(simkl_id, int):
        return None
    slug = ids.get("slug")
    return f"{SIMKL_ISSUER}/{section}/{simkl_id}/{slug if isinstance(slug, str) else ''}"


def _media_type(container: str, entry: dict[str, Any]) -> MediaType:
    if container == "movies":
        return "movie"
    if container == "anime" and entry.get("anime_type") == "movie":
        return "movie"
    return "series"


def parse_entries(body: object) -> SimklItems:
    """Parse a `/sync/all-items` or `/sync/ratings` body: `{"shows": [...], "movies": [...]}`.

    A key can be missing or `null`, and an empty library is `{}` (null-values convention), so
    every part is read defensively. Entries without a TMDB ID can't be matched to a title and
    are counted instead.
    """
    if not isinstance(body, dict):
        raise SimklError("Simkl returned an unexpected response.")
    entries: list[SimklEntry] = []
    skipped = 0
    for container in SIMKL_TYPES:
        rows = body.get(container)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            media = row.get("movie") or row.get("show") or row.get("anime")
            ids = media.get("ids") if isinstance(media, dict) else None
            tmdb_id = _tmdb_id(ids)
            status = row.get("status")
            if tmdb_id is None or not isinstance(media, dict) or not isinstance(status, str):
                skipped += 1
                continue
            year = media.get("year")
            title = media.get("title")
            section = {"movies": "movies", "shows": "tv", "anime": "anime"}[container]
            entries.append(
                SimklEntry(
                    media_type=_media_type(container, row),
                    tmdb_id=tmdb_id,
                    # Apps registered before client 45340 see `notinteresting` for `dropped`.
                    status="dropped" if status == "notinteresting" else status,
                    title=title.strip() if isinstance(title, str) and title.strip() else "",
                    year=year if isinstance(year, int) and not isinstance(year, bool) else None,
                    rating=_rating(row.get("user_rating")),
                    last_watched_at=parse_simkl_datetime(row.get("last_watched_at")),
                    url=_simkl_url(section, ids) if isinstance(ids, dict) else None,
                )
            )
    return SimklItems(entries=entries, skipped_without_tmdb=skipped)


def parse_id_list(body: object) -> SimklIdList:
    """Parse an `extended=ids_only` body into the titles it holds.

    The ID-only shape has no `anime_type`, so an anime counts as both a movie and a series with
    that TMDB ID. It only decides what to keep, so the worst case is keeping a stale row.
    """
    if not isinstance(body, dict):
        raise SimklError("Simkl returned an unexpected response.")
    titles: set[tuple[MediaType, int]] = set()
    for container in SIMKL_TYPES:
        rows = body.get(container)
        if not isinstance(rows, list):
            continue
        for row in rows:
            media = (
                (row.get("movie") or row.get("show") or row.get("anime"))
                if isinstance(row, dict)
                else None
            )
            tmdb_id = _tmdb_id(media.get("ids") if isinstance(media, dict) else None)
            if tmdb_id is None:
                continue
            if container == "movies":
                titles.add(("movie", tmdb_id))
            elif container == "shows":
                titles.add(("series", tmdb_id))
            else:
                titles.update((("movie", tmdb_id), ("series", tmdb_id)))
    return SimklIdList(titles=frozenset(titles))


class SimklClient:
    """Async Simkl client. Use `async with`, or call `aclose`."""

    def __init__(
        self,
        client_id: str,
        client_secret: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        install_log_redaction()
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.AsyncClient(base_url=API_BASE_URL, timeout=timeout)
        self._owns_http = http is None

    async def __aenter__(self) -> SimklClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # --- OAuth ---------------------------------------------------------------------------

    async def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str
    ) -> SimklTokens:
        """Trade an authorization code for tokens. Never retried: Simkl destroys the code on
        every attempt that gets past client authentication, so a second try can only fail.
        https://api.simkl.org/api-reference/oauth2-authorization-code
        """
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            grant_message="Simkl rejected the authorization. Connect again.",
        )

    async def refresh(self, refresh_token: str) -> SimklTokens:
        """Get a new access token. The refresh token is non-rotating (the same one comes
        back), and the old access token stops working at once, so only one process may refresh a
        grant. https://api.simkl.org/api-reference/oauth2-tokens
        """
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            grant_message="Simkl no longer accepts this connection. Reconnect Simkl.",
            grant_error=SimklAuthError,
        )

    async def revoke(self, token: str) -> None:
        """Revoke a grant (either token ends both). Simkl answers 200 whatever the token is, so
        success proves nothing about the token. https://api.simkl.org/api-reference/oauth2-tokens
        """
        resp = await self._post_form("/oauth2/revoke", {"token": token})
        if resp.status_code != 200:
            self._raise_oauth_error(resp, grant_message="Simkl could not revoke the token.")

    async def _token_request(
        self,
        data: dict[str, str],
        *,
        grant_message: str,
        grant_error: type[SimklError] = SimklError,
    ) -> SimklTokens:
        resp = await self._post_form("/oauth2/token", data)
        if resp.status_code != 200:
            self._raise_oauth_error(resp, grant_message=grant_message, grant_error=grant_error)
        body = self._json(resp)
        access = body.get("access_token") if isinstance(body, dict) else None
        refresh = body.get("refresh_token") if isinstance(body, dict) else None
        expires_in = body.get("expires_in") if isinstance(body, dict) else None
        scope = body.get("scope") if isinstance(body, dict) else None
        if (
            not isinstance(access, str)
            or not isinstance(refresh, str)
            or not access
            or not refresh
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not isinstance(scope, str)
        ):
            raise SimklError("Simkl returned an unexpected token response.")
        return SimklTokens(
            access_token=access, refresh_token=refresh, expires_in=expires_in, scope=scope
        )

    async def _post_form(self, path: str, data: dict[str, str]) -> httpx.Response:
        if not self._client_secret:
            raise SimklConfigError("The Simkl client secret is not configured.")
        try:
            # Server apps authenticate every token and revoke call, with the secret in an
            # HTTP Basic header. https://api.simkl.org/api-reference/auth
            return await self._http.post(
                path,
                data=data,
                auth=(self._client_id, self._client_secret),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except httpx.TimeoutException:
            raise SimklError("Timed out contacting Simkl.") from None
        except httpx.HTTPError:
            # No exception text: it can carry the request URL.
            raise SimklError("Network error contacting Simkl.") from None

    def _raise_oauth_error(
        self,
        resp: httpx.Response,
        *,
        grant_message: str,
        grant_error: type[SimklError] = SimklError,
    ) -> None:
        # `/oauth2/*` errors are `{"error": "<code>", "error_description": "..."}`; branch on the
        # code only. https://api.simkl.org/conventions/errors#oauth-20-errors
        code = self._error_code(resp)
        if resp.status_code == 401 or code == "invalid_client":
            raise SimklConfigError(
                "Simkl rejected this server's client credentials. "
                "Check SIMKL_CLIENT_ID and SIMKL_CLIENT_SECRET.",
                status_code=resp.status_code,
            )
        if code == "invalid_grant":
            raise grant_error(grant_message, status_code=resp.status_code)
        if resp.status_code >= 500:
            raise SimklError(f"Simkl is unavailable (HTTP {resp.status_code}).", resp.status_code)
        raise SimklError(f"Simkl rejected the request (HTTP {resp.status_code}).", resp.status_code)

    # --- Sync API ------------------------------------------------------------------------

    async def get_user_settings(self, access_token: str) -> SimklAccount:
        body = await self._get("/users/settings", access_token)
        account = body.get("account") if isinstance(body, dict) else None
        user = body.get("user") if isinstance(body, dict) else None
        account_id = account.get("id") if isinstance(account, dict) else None
        name = user.get("name") if isinstance(user, dict) else None
        return SimklAccount(
            account_id=account_id
            if isinstance(account_id, int) and not isinstance(account_id, bool)
            else None,
            name=name.strip() if isinstance(name, str) and name.strip() else None,
        )

    async def get_activities(self, access_token: str) -> dict[str, Any]:
        """The cheap "did anything change" call that gates every sync."""
        body = await self._get("/sync/activities", access_token)
        if not isinstance(body, dict):
            raise SimklError("Simkl returned an unexpected response.")
        return body

    async def get_all_items(
        self,
        access_token: str,
        simkl_type: SimklType | None = None,
        *,
        date_from: str | None = None,
    ) -> SimklItems:
        """Library entries, with `user_rating`. `date_from` limits it to entries changed after
        it, and is the `all` of the last snapshot exactly as Simkl returned it.
        https://api.simkl.org/api-reference/simkl/get-all-items
        """
        params = {"date_from": date_from} if date_from else None
        return parse_entries(
            await self._get(self._sync_path("all-items", simkl_type), access_token, params)
        )

    async def get_id_list(self, access_token: str, simkl_type: SimklType) -> SimklIdList:
        """Every title the user holds, IDs only. `date_from` can't surface removals, so the
        removal check compares this with what is stored. `ids_only` rather than
        `simkl_ids_only`, because the stored rows are keyed by TMDB ID.
        https://api.simkl.org/guides/sync#detecting-deletions
        """
        body = await self._get(
            self._sync_path("all-items", simkl_type), access_token, {"extended": "ids_only"}
        )
        return parse_id_list(body)

    async def get_ratings_delta(
        self, access_token: str, simkl_type: SimklType, *, date_from: str
    ) -> SimklItems:
        """Entries whose rating changed after `date_from`. Left without a rating segment, so
        removed ratings come back as `user_rating: null`. Call it only when `rated_at` moved.
        https://api.simkl.org/api-reference/simkl/get-user-ratings
        """
        return parse_entries(
            await self._get(
                self._sync_path("ratings", simkl_type), access_token, {"date_from": date_from}
            )
        )

    @staticmethod
    def _sync_path(endpoint: str, simkl_type: SimklType | None) -> str:
        return f"/sync/{endpoint}" + (f"/{simkl_type}" if simkl_type else "")

    async def _get(self, path: str, access_token: str, params: dict[str, str] | None = None) -> Any:
        query = {
            "client_id": self._client_id,
            "app-name": APP_NAME,
            "app-version": APP_VERSION,
            **(params or {}),
        }
        try:
            resp = await self._http.get(
                path,
                params=query,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
            )
        except httpx.TimeoutException:
            raise SimklError("Timed out contacting Simkl.") from None
        except httpx.HTTPError:
            raise SimklError("Network error contacting Simkl.") from None
        if resp.status_code != 200:
            self._raise_api_error(resp)
        return self._json(resp)

    def _raise_api_error(self, resp: httpx.Response) -> None:
        status = resp.status_code
        code = self._error_code(resp)
        if status == 401:
            # `user_token_failed` and `invalid_token` both mean the grant is no good (expired,
            # or the user removed the app). https://api.simkl.org/conventions/errors#unauthorized
            raise SimklAuthError("Simkl rejected the stored access token.", status)
        if status == 412:
            raise SimklConfigError(
                "Simkl rejected this server's client ID. Check SIMKL_CLIENT_ID.", status
            )
        if status == 429:
            retry_after = resp.headers.get("Retry-After", "")
            if code == "rate_limit":
                raise SimklRateLimitError(
                    "Simkl is rate limiting requests. Try again shortly.", daily=False
                )
            # The daily quota is per user and resets at midnight US Eastern.
            # https://api.simkl.org/resources/rate-limits
            raise SimklRateLimitError(
                "Simkl's daily request limit for this account is used up. "
                "It resets at midnight US Eastern.",
                daily=True,
                retry_after=int(retry_after) if retry_after.isdecimal() else None,
            )
        if status == 400 and code == "max_items":
            raise SimklConfigError("The Simkl library is too large to fetch.", status)
        if status == 403:
            raise SimklConfigError("Simkl refused the request (HTTP 403).", status)
        if status >= 500:
            raise SimklError(f"Simkl is unavailable (HTTP {status}).", status)
        raise SimklError(f"Simkl rejected the request (HTTP {status}).", status)

    @staticmethod
    def _error_code(resp: httpx.Response) -> str | None:
        try:
            body = resp.json()
        except ValueError:
            return None
        code = body.get("error") if isinstance(body, dict) else None
        return code if isinstance(code, str) else None

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise SimklError("Simkl returned an unreadable response.") from None
