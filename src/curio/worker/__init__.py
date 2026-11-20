"""Background worker process. A stub for now: it only proves DB connectivity.

The Postgres job queue arrives with #7.
"""

import asyncio
import contextlib
import logging

import psycopg
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from curio.db.engine import create_engine, create_session_factory
from curio.services import health as health_service
from curio.settings import Settings

log = logging.getLogger(__name__)


async def run(settings: Settings, stop: asyncio.Event) -> None:
    """Log a heartbeat every `worker_heartbeat_seconds` until `stop` is set."""
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        log.info("worker started")
        while not stop.is_set():
            try:
                async with session_factory() as session:
                    ok = await health_service.database_ok(session)
                log.info("heartbeat database=%s", "ok" if ok else "error")
            except (
                psycopg.OperationalError,
                SQLAlchemyOperationalError,
                SQLAlchemyTimeoutError,
            ) as exc:
                log.warning("heartbeat database=error: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_heartbeat_seconds)
    finally:
        await engine.dispose()
    log.info("worker stopped")
