# Architecture

Curio is one Python package (`src/curio`) run as three kinds of process that share one Postgres database. Decisions behind this design are in [`docs/adr/`](adr/).

## Processes

```
Nuvio / browser ──HTTP──▶ api (N uvicorn workers) ──┐
                                                    ├──▶ Postgres 16 + pgvector
                          worker (M processes) ─────┘
                          migrate (one-shot, before api/worker start)
```

- **api**: stateless FastAPI app (`uvicorn --factory curio.api.app:create_app`). Any number of uvicorn workers and containers can run, because no state that matters lives in the process (each holds one `LISTEN` connection and its own stream counters, see [Background jobs](#background-jobs)). Set the count with `API_WORKERS`. Uvicorn's own `--workers` manager (0.30+) restarts crashed workers, so gunicorn isn't needed. To scale across machines instead, set `API_WORKERS=1` and run more api containers.
- **worker**: background processes (`python -m curio.worker`) that claim and run jobs from the Postgres queue (see [Background jobs](#background-jobs)). Each logs a DB heartbeat and exits cleanly on SIGTERM. Set the count with `WORKER_REPLICAS` or `--scale worker=N`.
- **migrate**: applies schema migrations and exits. api and worker start only after it succeeds.

Catalog requests from Nuvio are served from stored rows only. LLM calls happen in workers, never on the request path.

## Layers and import rules

```
curio/api/routers  →  curio/services  →  curio/repositories  →  Postgres
                             ↓
                       curio/clients  →  external HTTP APIs
```

| Package | Role | May import |
| --- | --- | --- |
| `api/routers` | HTTP shape: parse, validate, call a service, format the response | `services`, `api/deps`, `api/schemas` |
| `api/schemas` | Pydantic request and response models, one module per domain (`<domain>.py`). Never expose ORM models or secret fields | `pydantic`, domain enums |
| `services` | Business logic | `repositories`, `clients` |
| `repositories` | All database queries. Functions take an `AsyncSession` as their first argument | `sqlalchemy` + `db/models` |
| `clients` | External HTTP (TMDB, Letterboxd RSS, Simkl, LLMs) | `httpx` only; never the DB |
| `worker` | Long-running jobs | `services` |
| `db` | Engine, session factory, declarative models, and Alembic migrations | |

Rules:
- Only `repositories` contain database queries. The exception is Alembic migrations.
- `clients` never touch the database. Services fetch with a client, then save with a repository.
- Public addon routes (`api/routers/addon.py`; unauthenticated, CORS `*`) and authenticated control-plane routes (`api/routers/control.py`, under `/api`) are separate routers so auth and CORS policy apply per router.
- No module-level DB state. `create_app()` builds the app; the lifespan creates an `AsyncEngine` and `async_sessionmaker` and stores them on `app.state`. Routes get a session through the `get_session` dependency (`api/deps.py`), which commits on success and rolls back on error. Code that needs an explicit transaction uses `session.begin_nested()` or a fresh session, never `session.commit()` inside a repository; inside the test fixture's outer transaction that becomes a savepoint, so test isolation still holds.
- ORM models never reach an HTTP response; every `/api/*` route declares explicit Pydantic request/response models in `src/curio/api/schemas/<domain>.py` (request and response for a domain in one file), mapped explicitly from repository/service results. Display queries never select `encrypted_key`, and no API key value or ciphertext field may appear in response schemas.

## Data ownership

- **Shared** data is the same for everyone and is cached once: title metadata from TMDB, and public taste sources (a diary RSS feed is fetched once, even if several users follow it).
- **Per-user** data belongs to one user: settings and encrypted API keys, the Simkl connection and its sealed tokens, presets, slot assignments, the user's role for each source, watch history, vetoes, and generated catalogs. Sources built from an uploaded export are per-user too: they have an `owner_user_id`, and nobody else can see or subscribe to them ([ADR 0009](adr/0009-source-ownership-and-history-provenance.md)). Every per-user table has `user_id` in its primary key, and every per-user repository function takes `user_id` as a required argument (#2).

## Migrations

Schema definitions live in SQLAlchemy 2.0 `Mapped[...]` models under `src/curio/db/models/`. Alembic configuration (`src/curio/db/alembic.ini`) and revisions (`src/curio/db/alembic/versions/`) live inside the package so the Docker image includes them. Run them with `alembic -c src/curio/db/alembic.ini upgrade head` (the compose `migrate` service does this). The app never migrates on startup.

`env.py` runs migrations asynchronously and takes a session-level `pg_advisory_lock`, so concurrent runs serialise. Re-running is a no-op, and `alembic check` in CI verifies that models and revisions agree. Existing databases created by the earlier SQL runner can be marked at the initial revision once with `alembic -c src/curio/db/alembic.ini stamp head`.

## Background jobs
The queue is the `jobs` table; there is no Redis. A job belongs to a user, usually to a preset, and has a `job_kind`, an optional JSON `payload` (never API keys), a `run_at` time and the `required_keys` it needs.
**States.** `queued` → `running` → `succeeded` | `failed`. A job can also be `paused` (waiting for a key) or `cancelled`. A partial unique index allows one `queued`, `running` or `paused` job per user, preset and kind, so the database rejects duplicates.
**Claiming and leases.** A worker claims a job with `SELECT ... FOR UPDATE SKIP LOCKED`. A job is claimable when it is `queued` and due, or `running` with an expired lease. The claim sets `claimed_by` and `lease_expires_at` (`JOB_LEASE_SECONDS`, default 120) and counts one attempt. While the job runs, a background task renews the lease every third of the lease length on its own short session. If a worker dies, the lease expires and another worker reclaims the job: it marks the old run `failed` ("Worker stopped responding.") and runs the job again, or fails the job if it is out of attempts. Every worker-side write (finish, lease renewal, pause) is conditioned on `status = 'running' AND claimed_by = <this worker>`, so a worker that lost its job cannot overwrite the new owner. It logs a warning and stops writing.
**Runs.** Each attempt gets its own `runs` row (`attempt`, pipeline, trigger, status, latency) and its own `run_events`. The UI shows the latest run.
**Retries.** An unexpected failure requeues the job with a backoff of 10 s × attempt until `max_attempts` (3), then marks it `failed`. Run errors, `jobs.last_error` and event messages are shown in the browser, so only messages from an allow-list of exception types (TMDB errors, missing or paused keys) are stored. Any other error is stored as "Unexpected error (<Class>). See worker logs." and logged in full.
**Pause and resume (ADR 0002 §5, ADR 0008 §8).** A TMDB 401 or 429 (or a missing key) pauses the key, the user's `queued` jobs that need that key, and the current job. The current job's attempt is handed back, so a pause doesn't count against `max_attempts`. Other `running` jobs pause themselves if they hit the same error. Enqueueing a job whose key is paused is rejected (409). Replacing the key resumes the paused jobs. Simkl is the one "key" that isn't a pasted API key: `KeyType.SIMKL` pauses on `simkl_connections.needs_reconnect`, and connecting again resumes the paused jobs ([ADR 0010](adr/0010-simkl-connection-and-revocation.md)).
**Job kinds.** `JOB_REGISTRY` in `services/jobs.py` maps a `job_kind` to a `JobSpec`: pipeline name, required key types, an async handler that receives a `JobContext` (session, session factory, settings, rate limiter, job, run, `emit_step`), and the success message. A new kind adds a handler and a registry entry, and extends the `jobs_job_kind_check` constraint in a migration. A job with an unregistered kind fails with a clear message. `regenerate` runs the `starter` pipeline, `lb_import` imports an uploaded Letterboxd export (see [Sources and import](#sources-and-import)), and `simkl_sync` and `simkl_revoke` run the Simkl connection (see [Simkl](#simkl)). A handler raises `PermanentJobError` for a failure that retrying can't fix (an invalid upload): the job fails at once and the message is shown to the user.
**Progress.** The handler calls `emit_step`, which writes a `run_events` row, sends `pg_notify('job_events', '{"job_id", "event_id", "status"}')` and commits. There is one constant channel. Each API process holds a single `JobEventHub` (`api/job_events.py`): one non-pooled connection that `LISTEN`s on `job_events` and wakes the SSE streams subscribed to that job id. It reconnects with backoff and wakes every subscriber when it does. A wake-up carries no data the stream trusts: the stream re-reads `run_events` after its last event id. This works with any number of API processes.
**SSE** (`GET /api/jobs/{id}/events`). The route authenticates and looks up the job on a short session, closes it, then streams, so an open stream holds no pooled database connection. Each step event carries `id: <run_event id>` and the route honours `Last-Event-ID`. A user may hold `SSE_MAX_STREAMS_PER_USER` (5) streams per API process (429 beyond that), and a stream ends after `SSE_MAX_STREAM_SECONDS` (1800); the browser then reconnects. A keepalive comment is sent every 15 s.

## Sources and import
A **source** is a typed input to taste (ADR 0003): a public diary RSS feed (`lb_diary_rss`), or a profile or list export (`lb_profile_csv`, `lb_list_csv`). Users subscribe to sources with a role (`self`, `taste`, `candidates`). Ratings are stored on Curio's 0-10 scale everywhere (Letterboxd stars x 2, converted when parsed). A Simkl connection is a fourth kind (`simkl`, owned by the user, see [Simkl](#simkl)); Simkl's 1-10 ratings are stored as they are.

**Ownership (ADR 0009).** Public sources have no owner, are keyed by feed, and are shared. Export sources have `owner_user_id` and are keyed by `(kind, owner, file)`, never by the username inside the file. Only the owner can see, change, refresh or delete an owned source (404 for anyone else), and removing one deletes it.

**Letterboxd RSS needs no TMDB key.** Feeds carry TMDB IDs, so the API (adding or refreshing a source) and the worker (polling) store `media_items` stubs with the feed's names and years, plus the source items. Stubs are hydrated later by the normal pipelines. A feed response is capped at 5 MB, streamed, and redirects must stay on `letterboxd.com`. The worker loop polls public RSS sources that have a subscriber and whose `next_refresh_at` has passed, up to 3 per iteration, so jobs are not starved. A poller claims a source with `SELECT ... FOR UPDATE SKIP LOCKED`, sets `next_refresh_at` to now plus `RSS_REFRESH_INTERVAL_HOURS` (default 6) and commits before fetching, so concurrent workers take different sources and a failing feed is retried after the interval. A failure is written to `sources.last_error` on its own session so the rollback of the ingest doesn't discard it. A manual refresh also sets `next_refresh_at`.

**ZIP import is a worker job.** Matching export rows needs the user's TMDB key, which only workers can decrypt (ADR 0002). `POST /api/sources/import-zip` reads the upload in chunks (413 over 50 MB), stores it in `source_uploads`, enqueues an `lb_import` job (`{upload_id, role}`) and returns 202 with the job. It answers 422 when no TMDB key exists and 409 when the key is paused or the user already has an import queued or running. The handler parses the ZIP (at most 1000 members, 20 MB per entry and 200 MB in total, counted from the bytes read), matches each distinct title and year against TMDB (movies first, TV only when there is no confident movie match), hydrates the matched titles, and replaces the user's export sources: titles that left the export are removed along with their ratings. Rows that match below 0.85 confidence, or tie, become import issues. Resolutions the user made earlier are applied again. The upload is deleted with the import's writes, or when the job fails for good; a paused job keeps it. The Starlette multipart parser has already received the body when the route runs, so run a reverse proxy that caps request bodies (for example `client_max_body_size 55m`) in front of the API.

**Import issues.** Resolving an issue needs no TMDB call: the API stores a stub from the chosen candidate (or the row's own title and year) and the source item.

**Watch history provenance.** `watch_history.source_id` names the source that wrote a row (null: manual). A source inserts missing rows and updates or deletes rows it owns, never anyone else's. `resync_watch_history(user)` makes a user's history match their `self` sources from stored items, with no network: it deletes rows owned by sources that are no longer `self`, deletes rows whose title a source dropped, then writes each `self` source in subscription order. It runs after every ingest, import, role change, unsubscribe and issue resolution. Within a source, `ratings.csv` outranks diary ratings, and the latest diary entry outranks earlier ones.

## Simkl
A user connects their own Simkl account (AUTH V2, authorization code with PKCE) and a worker syncs it into an owned `simkl` source ([ADR 0010](adr/0010-simkl-connection-and-revocation.md)). The API holds only the public key, so it can seal the tokens but never read them.
- **Connect.** `POST /api/simkl/connect` stores a hashed `state`, the session hash and a PKCE verifier in `simkl_oauth_states` (10 minutes) and returns Simkl's consent URL. `GET /api/simkl/callback` is the browser redirect. It needs the same session cookie, consumes the state in one `DELETE ... RETURNING`, checks `iss == https://simkl.com`, exchanges the code once, seals both tokens, and creates the source, a `self` subscription and a `simkl_sync` job. It always answers with a redirect to `/app/sources?simkl=connected|cancelled|error` and never puts a code or token in a URL. The redirect URI is `SIMKL_REDIRECT_URI` or `PUBLIC_BASE_URL` + `/api/simkl/callback`, never from the browser.
- **Sync (`simkl_sync`).** Refreshes the access token when it has a day or less left (the job holds a row lock, and commits the new token at once), reads `/sync/activities`, then pulls everything on the first run and only the moved buckets after that, with `date_from` = the saved snapshot's `all`. `rated_at` moving triggers the ratings delta, `removed_from_list` an `ids_only` diff. Items go to `source_items` (`watched`, `watching`, `dropped`; plan to watch and on hold are skipped; ratings stay on Simkl's 1-10) and only `resync_watch_history` writes watch history, so ADR 0009 applies and dropped series become anti-taste. The snapshot is saved in the transaction that commits the items, and `last_error` goes to the connection and the source on a separate session. Titles without a TMDB ID are skipped and counted. Nothing polls Simkl on a timer.
- **Rejected grants.** `invalid_grant` on refresh, or a 401 that survives one refresh, pauses the job with `KeyType.SIMKL` and sets `needs_reconnect`, and the UI asks the user to reconnect. A wrong client secret fails the job without asking the user to reconnect.
- **Disconnect.** `DELETE /api/simkl/connection` drops the source, its items and the history it wrote at once, and moves the sealed refresh token to `simkl_revocations`. A `simkl_revoke` job (payload: the row id) revokes it at Simkl, deletes the row, and discards it after the last failed attempt. Connecting again revokes the replaced grant the same way. The generic source routes refuse to change or remove a Simkl source.
- **Routes.** `GET /api/simkl/connection` (status, and the sync job to follow), `POST /api/simkl/sync` (202 with the job; 404 not connected; 409 already running or needs reconnect). The decrypt audit lists every worker read of a Simkl token with key type `simkl`.
- **Limits.** Simkl allows 10 GET a second and a daily quota per user (500 on a free plan). A sync with no changes costs one request, and a daily-quota 429 fails the job without retrying.
- **Fixtures.** `tests/fixtures/simkl/` is derived from the docs until `scripts/record_simkl_fixtures.py` is run against a real account.
## Testing

- `tests/unit/`: no database or network.
- `tests/integration/`: real Postgres. Uses `TEST_DATABASE_URL` if set, otherwise a throwaway `pgvector/pgvector:pg16` container via testcontainers. Migrations run once per session. Each test's `session` fixture joins an `AsyncSession` to an outer transaction that is rolled back afterwards. The `client` fixture is an `httpx.AsyncClient` against the app with `get_session` overridden to use that session. Routes that commit are invisible to other connections under it, so tests that need real commits (SSE authentication, commit-before-respond) use `committed_client`, which doesn't override `get_session`.
- Outbound HTTP is mocked with `respx` (the `respx_mock` fixture). Real network is blocked by pytest-socket; only localhost and unix sockets are allowed. Postgres traffic goes through libpq, which pytest-socket does not intercept.
- `evals/`: live-LLM evals, marked `eval` and excluded by default. Run with `uv run pytest -m eval`.
- CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push and PR against a pgvector service container.
