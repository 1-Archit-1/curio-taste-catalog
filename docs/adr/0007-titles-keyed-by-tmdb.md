# 7. Titles keyed by TMDB ID

Status: Accepted

## Context

Every per-user table that refers to a title (watch history, vetoes, catalog candidates) needs a stable title key. Two IDs are available:

- **TMDB IDs** come with the taste inputs. Letterboxd diary RSS includes them, and CSV exports are matched through TMDB search (ADR 0004). TMDB is also the metadata source. TMDB numbers movies and TV separately, so movie 807 and TV 807 are different titles.
- **IMDb IDs** (`tt...`) are what Stremio and Nuvio use to address items, so the addon manifest declares `idPrefixes: ["tt"]`. Some TMDB titles have no IMDb ID, mostly obscure films, shorts, and some series.

Keying by IMDb ID would require a TMDB-to-IMDb lookup before any history row could be stored. Titles without an IMDb ID could not be stored at all, so they would silently drop out of exclusions and anti-taste. A composite key of both IDs has the same problem, because primary-key columns cannot be null.

## Decision

- `media_items` is keyed by `(media_type, tmdb_id)`. `imdb_id` is a nullable, unique column.
- `watch_history`, `vetoes`, and `catalog_candidates` reference titles by `(media_type, tmdb_id)` through a composite foreign key. The `excluded_titles` and `anti_taste` views expose the same key.
- The addon catalog route serves only titles that have an `imdb_id`. Titles without one still count for exclusion and taste, and still appear as candidates in the control plane, where they can be vetoed.
- Control-plane veto routes address titles as `/api/vetoes/{media_type}/{tmdb_id}`.

## Alternatives considered

- **IMDb ID as the key.** Matches the addon protocol directly, but loses titles that have no IMDb ID and puts an extra lookup in front of every import.
- **A surrogate `media_items.id`**, with unique `(media_type, tmdb_id)` and unique `imdb_id` constraints. This would also cover titles that aren't on TMDB, and it would survive TMDB merging duplicate entries. It adds a level of indirection that isn't needed while all metadata comes from TMDB. Revisit it if a source (for example Simkl) introduces titles that TMDB doesn't have.

## Consequences

- Imports store history straight from TMDB IDs, with no IMDb lookup on the write path.
- A title can be excluded or count as anti-taste even if it can never be served to Nuvio.
- An IMDb ID is needed only to put a title into a served catalog row. The metadata fetch that fills `media_items` from TMDB is responsible for setting it.
