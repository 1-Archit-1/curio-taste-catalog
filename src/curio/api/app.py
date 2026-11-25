import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from curio import ADDON_NAME
from curio.api.job_events import JobEventHub, listen_dsn
from curio.api.routers import addon, control, health, simkl, ui
from curio.clients.redaction import install_access_log_redaction
from curio.db.engine import create_engine, create_session_factory
from curio.settings import Settings

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API app. Run with `uvicorn --factory curio.api.app:create_app`."""
    settings = settings or Settings()
    install_access_log_redaction()
    if settings.key_private_key:
        # ADR 0002: only worker processes may hold the private key. Settings also reads `.env`,
        # so a local run could otherwise pick it up silently.
        raise RuntimeError(
            "KEY_PRIVATE_KEY is set, but the API must never hold the private key (ADR 0002). "
            "Unset it in the API's environment and remove it from the .env the API reads."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        hub = JobEventHub(listen_dsn(engine))
        app.state.job_event_hub = hub
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()
            await engine.dispose()

    app = FastAPI(title=ADDON_NAME, lifespan=lifespan)
    app.state.settings = settings

    app.include_router(health.router)
    app.include_router(control.router)
    app.include_router(simkl.router)
    app.include_router(ui.router)
    app.include_router(addon.router)

    async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
        log.warning("database unavailable: %s", exc)
        return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)

    app.add_exception_handler(psycopg.OperationalError, database_unavailable)
    app.add_exception_handler(SQLAlchemyOperationalError, database_unavailable)
    app.add_exception_handler(SQLAlchemyTimeoutError, database_unavailable)

    return app
