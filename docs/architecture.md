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

- **Shared** data is the same for everyone and is cached once: title metadata from TMDB, and taste sources (a public diary RSS or a list is fetched once, even if several users follow it).
- **Per-user** data belongs to one user: settings and encrypted API keys, presets, slot assignments, the user's role for each source, watch history, vetoes, and generated catalogs. Every per-user table has `user_id` in its primary key, and every per-user repository function takes `user_id` as a required argument (#2).

## Migrations

Schema definitions live in SQLAlchemy 2.0 `Mapped[...]` models under `src/curio/db/models/`. Alembic configuration (`src/curio/db/alembic.ini`) and revisions (`src/curio/db/alembic/versions/`) live inside the package so the Docker image includes them. Run them with `alembic -c src/curio/db/alembic.ini upgrade head` (the compose `migrate` service does this). The app never migrates on startup.

`env.py` runs migrations asynchronously and takes a session-level `pg_advisory_lock`, so concurrent runs serialise. Re-running is a no-op, and `alembic check` in CI verifies that models and revisions agree. Existing databases created by the earlier SQL runner can be marked at the initial revision once with `alembic -c src/curio/db/alembic.ini stamp head`.

## Background jobs
The queue is the `jobs` table; there is no Redis. A job belongs to a user, usually to a preset, and has a `job_kind`, an optional JSON `payload` (never API keys), a `run_at` time and the `required_keys` it needs.
**States.** `queued` → `running` → `succeeded` | `failed`. A job can also be `paused` (waiting for a key) or `cancelled`. A partial unique index allows one `queued`, `running` or `paused` job per user, preset and kind, so the database rejects duplicates.
**Claiming and leases.** A worker claims a job with `SELECT ... FOR UPDATE SKIP LOCKED`. A job is claimable when it is `queued` and due, or `running` with an expired lease. The claim sets `claimed_by` and `lease_expires_at` (`JOB_LEASE_SECONDS`, default 120) and counts one attempt. While the job runs, a background task renews the lease every third of the lease length on its own short session. If a worker dies, the lease expires and another worker reclaims the job: it marks the old run `failed` ("Worker stopped responding.") and runs the job again, or fails the job if it is out of attempts. Every worker-side write (finish, lease renewal, pause) is conditioned on `status = 'running' AND claimed_by = <this worker>`, so a worker that lost its job cannot overwrite the new owner. It logs a warning and stops writing.
**Runs.** Each attempt gets its own `runs` row (`attempt`, pipeline, trigger, status, latency) and its own `run_events`. The UI shows the latest run.
**Retries.** An unexpected failure requeues the job with a backoff of 10 s × attempt until `max_attempts` (3), then marks it `failed`. Run errors, `jobs.last_error` and event messages are shown in the browser, so only messages from an allow-list of exception types (TMDB errors, missing or paused keys) are stored. Any other error is stored as "Unexpected error (<Class>). See worker logs." and logged in full.
**Pause and resume (ADR 0002 §5, ADR 0008 §8).** A TMDB 401 or 429 (or a missing key) pauses the key, the user's `queued` jobs that need that key, and the current job. The current job's attempt is handed back, so a pause doesn't count against `max_attempts`. Other `running` jobs pause themselves if they hit the same error. Enqueueing a job whose key is paused is rejected (409). Replacing the key resumes the paused jobs.
**Job kinds.** `JOB_REGISTRY` in `services/jobs.py` maps a `job_kind` to a `JobSpec`: pipeline name, required key types, an async handler that receives a `JobContext` (session, session factory, settings, rate limiter, job, run, `emit_step`), and the success message. A new kind adds a handler and a registry entry, and extends the `jobs_job_kind_check` constraint in a migration. A job with an unregistered kind fails with a clear message. `regenerate` runs the `starter` pipeline.
**Progress.** The handler calls `emit_step`, which writes a `run_events` row, sends `pg_notify('job_events', '{"job_id", "event_id", "status"}')` and commits. There is one constant channel. Each API process holds a single `JobEventHub` (`api/job_events.py`): one non-pooled connection that `LISTEN`s on `job_events` and wakes the SSE streams subscribed to that job id. It reconnects with backoff and wakes every subscriber when it does. A wake-up carries no data the stream trusts: the stream re-reads `run_events` after its last event id. This works with any number of API processes.
**SSE** (`GET /api/jobs/{id}/events`). The route authenticates and looks up the job on a short session, closes it, then streams, so an open stream holds no pooled database connection. Each step event carries `id: <run_event id>` and the route honours `Last-Event-ID`. A user may hold `SSE_MAX_STREAMS_PER_USER` (5) streams per API process (429 beyond that), and a stream ends after `SSE_MAX_STREAM_SECONDS` (1800); the browser then reconnects. A keepalive comment is sent every 15 s.

## Testing

- `tests/unit/`: no database or network.
- `tests/integration/`: real Postgres. Uses `TEST_DATABASE_URL` if set, otherwise a throwaway `pgvector/pgvector:pg16` container via testcontainers. Migrations run once per session. Each test's `session` fixture joins an `AsyncSession` to an outer transaction that is rolled back afterwards. The `client` fixture is an `httpx.AsyncClient` against the app with `get_session` overridden to use that session. Routes that commit are invisible to other connections under it, so tests that need real commits (SSE authentication, commit-before-respond) use `committed_client`, which doesn't override `get_session`.
- Outbound HTTP is mocked with `respx` (the `respx_mock` fixture). Real network is blocked by pytest-socket; only localhost and unix sockets are allowed. Postgres traffic goes through libpq, which pytest-socket does not intercept.
- `evals/`: live-LLM evals, marked `eval` and excluded by default. Run with `uv run pytest -m eval`.
- CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push and PR against a pgvector service container.
