"""Background worker process. A stub for now: it only proves DB connectivity.

The Postgres job queue arrives with #7.
"""

import asyncio
import contextlib
import logging

import psycopg

from curio.db.pool import create_pool
from curio.services import health as health_service
from curio.settings import Settings

log = logging.getLogger(__name__)


async def run(settings: Settings, stop: asyncio.Event) -> None:
    """Log a heartbeat every `worker_heartbeat_seconds` until `stop` is set."""
    async with create_pool(settings) as pool:
        log.info("worker started")
        while not stop.is_set():
            try:
                async with pool.connection() as conn:
                    ok = await health_service.database_ok(conn)
                log.info("heartbeat database=%s", "ok" if ok else "error")
            except psycopg.OperationalError as exc:
                log.warning("heartbeat database=error: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_heartbeat_seconds)
    log.info("worker stopped")
