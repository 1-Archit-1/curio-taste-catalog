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
| `repositories` | All SQL. Functions take a connection as their first argument | `psycopg` only |
| `clients` | External HTTP (TMDB, Letterboxd RSS, Simkl, LLMs) | `httpx` only; never the DB |
| `worker` | Long-running jobs | `services` |
| `db` | Pool construction and migration runner | |

Rules:
- Only `repositories` contain SQL. The exception is the migration runner and the `.sql` files it applies.
- `clients` never touch the database. Services fetch with a client, then save with a repository.
- Public addon routes (`api/routers/addon.py`; unauthenticated, CORS `*`) and authenticated control-plane routes (`api/routers/control.py`, under `/api`) are separate routers so auth and CORS policy apply per router.
- No module-level DB state. `create_app()` builds the app; the lifespan opens an `AsyncConnectionPool` and stores it on `app.state.pool`. Routes get a connection through the `get_conn` dependency (`api/deps.py`), which commits on success and rolls back on error. Code that needs an explicit transaction uses `async with conn.transaction():` and never calls `conn.commit()`; inside the test fixture's transaction that becomes a savepoint, so test isolation still holds.

## Data ownership

- **Shared** data is the same for everyone and is cached once: title metadata from TMDB, and taste sources (a public diary RSS or a list is fetched once, even if several users follow it).
- **Per-user** data belongs to one user: settings and encrypted API keys, presets, slot assignments, the user's role for each source, watch history, vetoes, and generated catalogs. Every per-user table has `user_id` in its primary key, and every per-user repository function takes `user_id` as a required argument (#2).

## Migrations

Migrations are numbered plain SQL files in `src/curio/db/migrations/` named `NNNN_description.sql`. Run them with `python -m curio.db.migrate` (the compose `migrate` service does this). The app never migrates on startup.

The runner takes a session-level `pg_advisory_lock`, so concurrent runs serialise. It records applied versions in `schema_migrations` and applies each pending file in its own transaction, so a failure leaves that migration unapplied. Re-running is a no-op. Migrations are forward-only. Statements that cannot run in a transaction (e.g. `CREATE INDEX CONCURRENTLY`) are not supported yet.

## Testing

- `tests/unit/`: no database or network.
- `tests/integration/`: real Postgres. Uses `TEST_DATABASE_URL` if set, otherwise a throwaway `pgvector/pgvector:pg16` container via testcontainers. Migrations run once per session. Each test's `conn` fixture is a transaction that is rolled back afterwards. The `client` fixture is an `httpx.AsyncClient` against the app with `get_conn` overridden to use that connection.
- Outbound HTTP is mocked with `respx` (the `respx_mock` fixture). Real network is blocked by pytest-socket; only localhost and unix sockets are allowed. Postgres traffic goes through libpq, which pytest-socket does not intercept.
- `evals/`: live-LLM evals, marked `eval` and excluded by default. Run with `uv run pytest -m eval`.
- CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push and PR against a pgvector service container.
