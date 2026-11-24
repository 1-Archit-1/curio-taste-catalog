#!/usr/bin/env python3
"""Record Simkl API responses as test fixtures, from a real account. Needs a human.

It runs the AUTH V2 code flow on loopback: it listens on
`http://localhost:<port>/api/simkl/callback`, prints the consent URL for you to open, exchanges
the code, records the responses the sync uses, scrubs them, writes them to
`tests/fixtures/simkl/`, and revokes the token.

    SIMKL_CLIENT_ID=... SIMKL_CLIENT_SECRET=... uv run python scripts/record_simkl_fixtures.py

Register `http://localhost/api/simkl/callback` as a redirect URI on a "Server apps & services"
app first. Simkl ignores the port of a loopback URI when authorizing, but the token exchange
compares the full string, so this script sends the same URI, port included, to both.

The files it writes are the ones the tests load (see tests/fixtures/simkl/README.md), so
recording replaces the doc-derived fixtures. Tests that assert specific titles and ratings were
written against those, so review `git diff tests/fixtures/simkl` and update what the real data
changes. Everything that identifies the account (tokens, account id, name, email, avatar) is
replaced with stable fakes, and nothing secret is printed or written.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from curio.clients.simkl import (
    API_BASE_URL,
    APP_NAME,
    APP_VERSION,
    SIMKL_ISSUER,
    USER_AGENT,
    build_authorize_url,
    generate_pkce,
)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "simkl"
DEFAULT_PORT = 8910
CALLBACK_PATH = "/api/simkl/callback"
FAKE_ACCESS_TOKEN = "simkl_at_FIXTUREACCESSTOKEN000000000000000000"
FAKE_REFRESH_TOKEN = "simkl_rt_FIXTUREREFRESHTOKEN00000000000000000"
FAKE_ACCOUNT_ID = 424242
FAKE_USERNAME = "curio-fixture-user"
# The library calls to record: (file name, path, query parameters).
RATINGS_DATE_FROM = "1970-01-01T00:00:00Z"
TYPES = ("shows", "movies", "anime")
# Keys that identify a person, wherever they appear in a response.
_PRIVATE_KEYS = {
    "avatar": "https://example.invalid/avatar.jpg",
    "email": "user@example.invalid",
    "bio": "",
    "loc": "",
    "gender": "",
    "age": None,
    "joined_at": "2020-01-01T00:00:00Z",
    "name": FAKE_USERNAME,
    "username": FAKE_USERNAME,
}


def scrub(
    value: Any,
    *,
    secrets_to_hide: tuple[str, ...] = (),
    names_to_hide: tuple[str, ...] = (),
    under: str = "",
) -> Any:
    """Replace everything that identifies the account. Pure; returns a scrubbed copy.

    `name` is only private inside `user` (a library entry's `title` is public data), and
    `account.id` becomes a fake id. As a last line of defence, a string that contains a secret
    has it replaced, and a string that is exactly the real username becomes the fake one (never a
    substring: "Alice in Wonderland" is a title, not a leak).
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if (under == "user" and key in _PRIVATE_KEYS) or key in {"avatar", "email"}:
                out[key] = _PRIVATE_KEYS[key]
            elif under == "account" and key == "id":
                out[key] = FAKE_ACCOUNT_ID
            else:
                out[key] = scrub(
                    item, secrets_to_hide=secrets_to_hide, names_to_hide=names_to_hide, under=key
                )
        return out
    if isinstance(value, list):
        return [
            scrub(item, secrets_to_hide=secrets_to_hide, names_to_hide=names_to_hide, under=under)
            for item in value
        ]
    if isinstance(value, str):
        if value in names_to_hide:
            return FAKE_USERNAME
        text = value
        for secret in secrets_to_hide:
            if secret and secret in text:
                text = text.replace(secret, "REDACTED")
        return text
    return value


def write_fixture(out_dir: Path, name: str, body: Any) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(
        json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


class _Callback:
    """Collects the one request Simkl's redirect makes to the local server."""

    def __init__(self) -> None:
        self.params: dict[str, str] | None = None
        self.received = threading.Event()


def _serve(port: int, callback: _Callback) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            parts = urlsplit(self.path)
            if parts.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            callback.params = {k: v[0] for k, v in parse_qs(parts.query).items()}
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Simkl fixtures: you can close this tab.")
            callback.received.set()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # the request line holds the one-time code

    server = HTTPServer(("localhost", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _basic(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


def record(client_id: str, client_secret: str, port: int, out_dir: Path, wait_seconds: int) -> int:
    redirect_uri = f"http://localhost:{port}{CALLBACK_PATH}"
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    callback = _Callback()
    server = _serve(port, callback)
    print("Register this redirect URI at Simkl, if you haven't: http://localhost" + CALLBACK_PATH)
    print("\nOpen this URL in your browser and approve access:\n")
    print(
        build_authorize_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
        )
    )
    print(f"\nWaiting up to {wait_seconds} s for the redirect to {redirect_uri} ...")
    try:
        if not callback.received.wait(wait_seconds) or callback.params is None:
            print("Timed out waiting for the redirect.", file=sys.stderr)
            return 1
    finally:
        server.shutdown()
    params = callback.params
    if params.get("iss") != SIMKL_ISSUER:
        print("Rejected: the response did not come from Simkl (iss).", file=sys.stderr)
        return 1
    if params.get("state") != state:
        print("Rejected: state mismatch.", file=sys.stderr)
        return 1
    if "error" in params or "code" not in params:
        print(f"Authorization failed: {params.get('error', 'no code')}", file=sys.stderr)
        return 1

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(base_url=API_BASE_URL, timeout=60, headers=headers) as http:
        # One attempt: Simkl destroys the code even when the exchange fails.
        token_resp = http.post(
            "/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            headers={"Authorization": _basic(client_id, client_secret)},
        )
        if token_resp.status_code != 200:
            print(f"Token exchange failed (HTTP {token_resp.status_code}).", file=sys.stderr)
            return 1
        tokens = token_resp.json()
        access, refresh = tokens["access_token"], tokens["refresh_token"]
        print(f"Granted scope: {tokens.get('scope')}")
        hidden = (access, refresh, client_secret)
        try:
            return _record_library(http, client_id, access, hidden, tokens, out_dir)
        finally:
            revoke = http.post(
                "/oauth2/revoke",
                data={"token": refresh},
                headers={"Authorization": _basic(client_id, client_secret)},
            )
            print(f"Revoked the token (HTTP {revoke.status_code}).")


def _record_library(
    http: httpx.Client,
    client_id: str,
    access: str,
    hidden: tuple[str, ...],
    tokens: dict[str, Any],
    out_dir: Path,
) -> int:
    base = {"client_id": client_id, "app-name": APP_NAME, "app-version": APP_VERSION}
    auth = {"Authorization": f"Bearer {access}"}

    def get(path: str, **extra: str) -> Any:
        resp = http.get(path, params={**base, **extra}, headers=auth)
        print(f"GET {path}{' ' + str(extra) if extra else ''} -> HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    # The account first, so its name can be hidden wherever it turns up afterwards.
    settings = get("/users/settings")
    name = (settings.get("user") or {}).get("name")
    names = (name,) if isinstance(name, str) and name else ()

    def save(name: str, body: Any) -> None:
        path = write_fixture(
            out_dir, name, scrub(body, secrets_to_hide=hidden, names_to_hide=names)
        )
        print(f"  wrote {path.name}")

    save("users_settings.json", settings)
    # Read the activities before the library, as a sync does.
    save("activities.json", get("/sync/activities"))
    for simkl_type in TYPES:  # one at a time: Simkl asks for sequential calls
        save(f"all_items_{simkl_type}.json", get(f"/sync/all-items/{simkl_type}"))
    for simkl_type in TYPES:
        save(
            f"ratings_{simkl_type}.json",
            get(f"/sync/ratings/{simkl_type}", date_from=RATINGS_DATE_FROM),
        )
    for simkl_type in TYPES:
        save(
            f"ids_only_{simkl_type}.json",
            get(f"/sync/all-items/{simkl_type}", extended="ids_only"),
        )
    save(
        "oauth_token.json",
        {
            "access_token": FAKE_ACCESS_TOKEN,
            "token_type": "Bearer",
            "expires_in": tokens.get("expires_in"),
            "refresh_token": FAKE_REFRESH_TOKEN,
            "scope": tokens.get("scope"),
        },
    )
    print(
        f"\nRecorded at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Review the diff before committing."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port to listen on")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="fixture directory")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for the redirect")
    args = parser.parse_args()
    client_id = os.environ.get("SIMKL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SIMKL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Set SIMKL_CLIENT_ID and SIMKL_CLIENT_SECRET.", file=sys.stderr)
        return 2
    return record(client_id, client_secret, args.port, args.out, args.wait)


if __name__ == "__main__":
    raise SystemExit(main())
