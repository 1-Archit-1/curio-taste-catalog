"""Background worker process: polls the Postgres job queue and executes jobs."""

import asyncio
import contextlib
import logging
import os
import socket
import time

import psycopg
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from curio.db.engine import create_engine, create_session_factory
from curio.services import health as health_service
from curio.services import jobs as jobs_service
from curio.services import media as media_service
from curio.settings import Settings

log = logging.getLogger(__name__)

_DATABASE_ERRORS = (
    psycopg.OperationalError,
    SQLAlchemyOperationalError,
    SQLAlchemyTimeoutError,
)


async def run(settings: Settings, stop: asyncio.Event) -> None:
    """Poll and process queued jobs until `stop` is set."""
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    rate_limiter = media_service.build_rate_limiter(settings)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    poll_timeout = min(settings.worker_heartbeat_seconds, 1.0)
    last_heartbeat = 0.0
    try:
        log.info("worker started worker_id=%s", worker_id)
        while not stop.is_set():
            try:
                now = time.monotonic()
                if now - last_heartbeat >= settings.worker_heartbeat_seconds:
                    async with session_factory() as session:
                        ok = await health_service.database_ok(session)
                    log.info("heartbeat database=%s", "ok" if ok else "error")
                    last_heartbeat = now

                while not stop.is_set():
                    try:
                        processed = await jobs_service.process_one_job(
                            session_factory,
                            settings=settings,
                            rate_limiter=rate_limiter,
                            worker_id=worker_id,
                        )
                    except _DATABASE_ERRORS:
                        raise
                    except Exception:
                        # One bad job must not kill the process. A job left `running` is
                        # reclaimed once its lease expires.
                        log.exception("worker: unexpected error while processing a job")
                        break
                    if not processed:
                        break
            except _DATABASE_ERRORS as exc:
                log.warning("worker database error: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_timeout)
    finally:
        await engine.dispose()
    log.info("worker stopped")
