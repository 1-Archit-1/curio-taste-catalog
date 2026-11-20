import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from curio import ADDON_NAME
from curio.api.routers import addon, control, health, ui
from curio.db.pool import create_pool
from curio.settings import Settings

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API app. Run with `uvicorn --factory curio.api.app:create_app`."""
    settings = settings or Settings()
    if settings.key_private_key:
        # ADR 0002: only worker processes may hold the private key. Settings also reads `.env`,
        # so a local run could otherwise pick it up silently.
        raise RuntimeError(
            "KEY_PRIVATE_KEY is set, but the API must never hold the private key (ADR 0002). "
            "Unset it in the API's environment and remove it from the .env the API reads."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(settings)
        await pool.open()
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title=ADDON_NAME, lifespan=lifespan)
    app.state.settings = settings

    app.include_router(health.router)
    app.include_router(control.router)
    app.include_router(ui.router)
    app.include_router(addon.router)

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(request: Request, exc: psycopg.OperationalError):
        # Covers connection failures and pool timeouts (PoolTimeout subclasses it).
        log.warning("database unavailable: %s", exc)
        return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)

    return app
