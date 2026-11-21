# Architecture

Curio is one Python package (`src/curio`) run as three kinds of process that share one Postgres database. Decisions behind this design are in [`docs/adr/`](adr/).

## Processes

```
Nuvio / browser ──HTTP──▶ api (N uvicorn workers) ──┐
                                                    ├──▶ Postgres 16 + pgvector
                          worker (M processes) ─────┘
                          migrate (one-shot, before api/worker start)
```

- **api**: stateless FastAPI app (`uvicorn --factory curio.api.app:create_app`). Any number of uvicorn workers and containers can run, because no state lives in the process. Set the count with `API_WORKERS`. Uvicorn's own `--workers` manager (0.30+) restarts crashed workers, so gunicorn isn't needed. To scale across machines instead, set `API_WORKERS=1` and run more api containers.
- **worker**: background processes (`python -m curio.worker`). Currently a stub that logs a DB heartbeat and exits cleanly on SIGTERM. The job queue (#7) will have workers claim jobs with `SELECT ... FOR UPDATE SKIP LOCKED` and report progress via `NOTIFY`, which lets any api process stream it. Set the count with `WORKER_REPLICAS` or `--scale worker=N`.
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
| `api/routers` | HTTP shape: parse, validate, call a service, format the response | `services`, `api/deps` |
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
- ORM models never reach an HTTP response; routes map to Pydantic schemas/responses explicitly, and display queries never select `encrypted_key`.

## Data ownership

- **Shared** data is the same for everyone and is cached once: title metadata from TMDB, and taste sources (a public diary RSS or a list is fetched once, even if several users follow it).
- **Per-user** data belongs to one user: settings and encrypted API keys, presets, slot assignments, the user's role for each source, watch history, vetoes, and generated catalogs. Every per-user table has `user_id` in its primary key, and every per-user repository function takes `user_id` as a required argument (#2).

## Migrations

Schema definitions live in SQLAlchemy 2.0 `Mapped[...]` models under `src/curio/db/models/`. Alembic configuration (`src/curio/db/alembic.ini`) and revisions (`src/curio/db/alembic/versions/`) live inside the package so the Docker image includes them. Run them with `alembic -c src/curio/db/alembic.ini upgrade head` (the compose `migrate` service does this). The app never migrates on startup.

`env.py` runs migrations asynchronously and takes a session-level `pg_advisory_lock`, so concurrent runs serialise. Re-running is a no-op, and `alembic check` in CI verifies that models and revisions agree. Existing databases created by the earlier SQL runner can be marked at the initial revision once with `alembic -c src/curio/db/alembic.ini stamp head`.

## Testing

- `tests/unit/`: no database or network.
- `tests/integration/`: real Postgres. Uses `TEST_DATABASE_URL` if set, otherwise a throwaway `pgvector/pgvector:pg16` container via testcontainers. Migrations run once per session. Each test's `session` fixture joins an `AsyncSession` to an outer transaction that is rolled back afterwards. The `client` fixture is an `httpx.AsyncClient` against the app with `get_session` overridden to use that session.
- Outbound HTTP is mocked with `respx` (the `respx_mock` fixture). Real network is blocked by pytest-socket; only localhost and unix sockets are allowed. Postgres traffic goes through libpq, which pytest-socket does not intercept.
- `evals/`: live-LLM evals, marked `eval` and excluded by default. Run with `uv run pytest -m eval`.
- CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push and PR against a pgvector service container.
