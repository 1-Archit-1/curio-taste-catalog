"""Re-record the TMDB lookup fixtures in `tests/fixtures/tmdb/` from the live API.

Usage (the key is read from the environment and is never logged or written):

    TMDB_API_KEY=... uv run python scripts/record_tmdb_fixtures.py [--out DIR] [NAME ...]

Files are written as pretty JSON with sorted keys. The only edits applied to a response are the
`results` and credits trims listed for it in `RECORDINGS` (documented in
`tests/fixtures/tmdb/README.md`).
The `api_key` query parameter is not part of any response, and this script never stores a
request URL, so the key cannot end up in a fixture.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "tmdb"


@dataclass(frozen=True)
class Recording:
    name: str
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    # Keep only the first `keep_results` entries of `results`. None keeps everything.
    keep_results: int | None = None
    # Title-detail trim: cast to the first 12, crew to every director plus others up to 5 total.
    trim_credits: bool = False


DETAILS = {"append_to_response": "external_ids,keywords,credits"}

RECORDINGS = [
    Recording("genres_movie.json", "/genre/movie/list"),
    Recording("genres_tv.json", "/genre/tv/list"),
    Recording("search_keyword.json", "/search/keyword", {"query": "dystopia", "page": 1}, 5),
    Recording("search_person.json", "/search/person", {"query": "Keanu", "page": 1}, 3),
    Recording("similar_movie_603.json", "/movie/603/similar", {"page": 1}, 2),
    # The two titles in similar_movie_603.json, hydrated by the integration tests.
    Recording("movie_647763.json", "/movie/647763", DETAILS, trim_credits=True),
    Recording("movie_4917.json", "/movie/4917", DETAILS, trim_credits=True),
]


def _trim_credits(credits: dict[str, Any]) -> None:
    credits["cast"] = credits["cast"][:12]
    keep = [c.get("job") == "Director" for c in credits["crew"]]
    room = 5 - sum(keep)
    for i, directing in enumerate(keep):
        if not directing and room > 0:
            keep[i] = True
            room -= 1
    credits["crew"] = [c for c, k in zip(credits["crew"], keep, strict=True) if k]


def record(client: httpx.Client, key: str, rec: Recording) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    params = dict(rec.params)
    # v4 Read Access Tokens are JWTs; v3 keys are short hex strings (same rule as TmdbClient).
    if len(key) > 64 or "." in key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    resp = client.get(f"{BASE_URL}{rec.path}", params=params, headers=headers)
    if resp.status_code != 200:
        # Report the status only: error text and URLs can carry the key.
        raise SystemExit(f"{rec.name}: TMDB returned HTTP {resp.status_code}")
    data = resp.json()
    if rec.keep_results is not None:
        data["results"] = data["results"][: rec.keep_results]
    if rec.trim_credits:
        _trim_credits(data["credits"])
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("names", nargs="*", help="fixture file names (default: all)")
    args = parser.parse_args()
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        raise SystemExit("TMDB_API_KEY is not set")
    unknown = set(args.names) - {r.name for r in RECORDINGS}
    if unknown:
        raise SystemExit(f"unknown fixture(s): {', '.join(sorted(unknown))}")
    selected = [r for r in RECORDINGS if not args.names or r.name in args.names]
    args.out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=10.0) as client:
        for rec in selected:
            data = record(client, key, rec)
            text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            if key in text:
                raise SystemExit(f"{rec.name}: response contains the API key; not written")
            (args.out / rec.name).write_text(text)
            print(f"wrote {rec.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
