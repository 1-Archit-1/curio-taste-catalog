import asyncio
import logging
import signal

from curio.settings import Settings
from curio.worker import run


async def _main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


if __name__ == "__main__":
    asyncio.run(_main())
