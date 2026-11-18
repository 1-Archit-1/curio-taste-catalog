import asyncio
import logging

from curio.settings import Settings
from curio.worker import run


async def test_worker_heartbeats_and_stops_cleanly(migrated_database_url, caplog):
    caplog.set_level(logging.INFO, logger="curio.worker")
    settings = Settings(
        _env_file=None, database_url=migrated_database_url, worker_heartbeat_seconds=0.05
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(settings, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("heartbeat database=ok") >= 2
    assert messages[-1] == "worker stopped"
