These files are real TMDB v3 responses, recorded on 2026-11-21. They are not hand-edited. The only changes are the trims listed below, so field names, types, nesting and the remaining values are TMDB's own. The `api_key` query parameter is never stored.

Tests load them offline through `respx`. To re-record, request the same endpoint and params with your own key, apply the same trims, and update the expected values in `tests/unit/test_tmdb_client.py` and `tests/integration/test_media_cache.py`.

Trims that apply to every title-detail file (`movie_*.json`, `tv_*.json`): `credits.cast` is cut to the first 12 entries, and `credits.crew` is cut to all directors (movies) plus other crew up to 5 entries in total. Nothing else is trimmed or changed.

| File | Request | Trimmed or changed |
| --- | --- | --- |
| `movie_603.json` | `GET /3/movie/603?append_to_response=external_ids,keywords,credits` (The Matrix) | Cast and crew only |
| `tv_1396.json` | `GET /3/tv/1396?append_to_response=external_ids,keywords,credits` (Breaking Bad) | Cast and crew only. `episode_run_time` is `[]` in the real response |
| `search_movie.json` | `GET /3/search/movie?query=The Matrix&year=1999` | Nothing. The response had 3 results |
| `discover_movie.json` | `GET /3/discover/movie?with_genres=27&sort_by=popularity.asc&page=1` | `results` is cut to two titles: the first with an IMDb ID (1836957, Mama Bear) and the first with no IMDb ID and zero votes (1836920, Deadline), in their original order |
| `recommendations_movie_603.json` | `GET /3/movie/603/recommendations?page=1` | `results` is cut to the first 2 of the page (87101, 777443) |
| `movie_1836920.json` | `GET /3/movie/1836920?append_to_response=external_ids,keywords,credits` (Deadline) | Cast and crew only. No IMDb ID, `vote_count` 0, no keywords |
| `movie_1836957.json` | `GET /3/movie/1836957?append_to_response=external_ids,keywords,credits` (Mama Bear) | Cast and crew only. Has an IMDb ID, `vote_count` 0, and an empty `release_date` |
| `movie_87101.json` | `GET /3/movie/87101?append_to_response=external_ids,keywords,credits` (Terminator Genisys) | Cast and crew only |
| `movie_777443.json` | `GET /3/movie/777443?append_to_response=external_ids,keywords,credits` (The Electric State) | Cast and crew only |

The discover and recommendation results are cut to titles that also have a recorded detail file, because the integration tests hydrate every returned title.
