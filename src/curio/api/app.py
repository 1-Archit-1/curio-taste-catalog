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
