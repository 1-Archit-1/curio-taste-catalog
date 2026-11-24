# Simkl fixtures

**Doc-derived. Re-record with `scripts/record_simkl_fixtures.py`.**

Nothing here was recorded from the live API yet. Every file is built from the response shapes in
the Simkl API docs (https://api.simkl.org), with fake titles, ids and accounts. The recorder
writes the same file names, so re-recording is a drop-in replacement. Run it, review
`git diff tests/fixtures/simkl`, and fix any test the real shapes break. That is the point of
re-recording.

The sync tests assert specific titles, statuses and ratings (for example Breaking Bad, TMDB 1396,
rated 10), so a recording of a real library changes what they expect. Update those expectations
by hand, or keep the doc-derived files and record into another directory with `--out`.

```sh
SIMKL_CLIENT_ID=... SIMKL_CLIENT_SECRET=... uv run python scripts/record_simkl_fixtures.py
```

| File | Endpoint |
| --- | --- |
| `activities.json` | `GET /sync/activities` |
| `all_items_shows.json`, `all_items_movies.json`, `all_items_anime.json` | `GET /sync/all-items/{type}` (the first full pull) |
| `ratings_shows.json`, `ratings_movies.json`, `ratings_anime.json` | `GET /sync/ratings/{type}?date_from=...` (no rating segment) |
| `ids_only_shows.json`, `ids_only_movies.json`, `ids_only_anime.json` | `GET /sync/all-items/{type}?extended=ids_only` |
| `users_settings.json` | `GET /users/settings` |
| `oauth_token.json` | `POST /oauth2/token` (fake tokens) |

What the doc-derived data contains on purpose:

- every status: `completed`, `dropped`, `watching`, `plantowatch`, `hold`
- one entry per type without a TMDB id (`ids.tmdb` missing), which the sync skips and counts
- an anime movie (`anime_type: "movie"`) and two anime entries that share a TMDB id
- a ratings row with `user_rating: null` (a removed rating)
- `ids.tmdb` as a string, as Simkl sends every id except `simkl`

Assumption to verify on the first recording: that the default `all-items` response carries
`ids.tmdb`. The docs' examples show it, but the schema text says external ids need
`extended=ids_only`.
