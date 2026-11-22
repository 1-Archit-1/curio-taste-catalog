# curio-taste-catalog

A self-hosted Stremio/Nuvio addon that serves AI-curated movie and series rows built from your own taste: Letterboxd exports and RSS, Simkl watch history, and friends' feeds. Generation runs as a LangGraph agent in a background worker; the catalog endpoints Nuvio calls never touch an LLM. The addon's display name is **Curio**.

Status: project skeleton. See the [milestones](https://github.com/1-Archit-1/curio-taste-catalog/milestones) and [issues](https://github.com/1-Archit-1/curio-taste-catalog/issues). Design: [`docs/architecture.md`](docs/architecture.md) and [`docs/adr/`](docs/adr/).

## Quickstart

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker with Compose v2 (`docker compose`).

```sh
uv sync                      # create .venv and install dependencies (incl. dev tools)
cp .env.example .env         # optional: change ports, counts, password
docker compose up -d --build # postgres, migrate (one-shot), api, worker
curl http://127.0.0.1:8000/healthz
docker compose logs -f worker
docker compose down          # add -v to also delete the database volume
```


Process counts come from `.env`: `API_WORKERS` (uvicorn workers in the api container) and `WORKER_REPLICAS` (worker containers). `docker compose up -d --scale worker=3` also works.

### Migrations

Schema migrations use Alembic (`src/curio/db/alembic.ini` and `src/curio/db/alembic/versions/`). The `migrate` service runs `alembic -c src/curio/db/alembic.ini upgrade head` before api and worker start. To run them by hand (safe to repeat):

```sh
docker compose run --rm migrate
# or from the host, against the compose Postgres:
DATABASE_URL=postgresql://curio:curio@localhost:5432/curio uv run alembic -c src/curio/db/alembic.ini upgrade head
```

If you have an existing database created by the earlier SQL migration runner (`0001` + `0002`), stamp it once so Alembic knows it is already at the initial revision:

```sh
DATABASE_URL=postgresql://curio:curio@localhost:5432/curio uv run alembic -c src/curio/db/alembic.ini stamp head
```

### Running locally without containers for the app

```sh
docker compose up -d postgres
export DATABASE_URL=postgresql://curio:curio@localhost:5432/curio  # or set it in .env
uv run alembic -c src/curio/db/alembic.ini upgrade head
uv run uvicorn --factory curio.api.app:create_app --reload
uv run python -m curio.worker
```

### Frontend (control plane)

The control-plane UI is a React + TypeScript app in `frontend/` built with Vite and served by FastAPI under `/app/` (`/` redirects to `/app/`). Requires Node 22+ for local development and builds (the production Docker image builds the frontend in a separate stage and contains no Node at runtime).

```sh
cd frontend
npm ci
npm run dev       # Vite dev server on http://127.0.0.1:5173/app/ (proxies /api to uvicorn :8000)
```

To build static assets for FastAPI to serve directly at `http://127.0.0.1:8000/app/`:

```sh
npm --prefix frontend run build
```

Whenever `/api/*` Pydantic schemas change, regenerate and commit the TypeScript API types (CI fails if `frontend/src/api/schema.d.ts` is stale):

```sh
npm --prefix frontend run gen:api
```

## Tests and lint

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest                 # unit + integration; evals excluded
uv run pytest tests/unit      # no database needed
uv run pytest -m eval         # live-LLM evals (opt-in, needs network and keys)

npm --prefix frontend run lint      # Biome lint, format, and import-order check
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
```

Integration tests use `TEST_DATABASE_URL` if set. Otherwise they start a throwaway `pgvector/pgvector:pg16` container with testcontainers, which needs Docker. Tests block real network access; mock outbound HTTP with the `respx_mock` fixture.
