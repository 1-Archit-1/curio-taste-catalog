# One image for the api, worker, and migrate services.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 curio
USER curio

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "--factory", "curio.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
