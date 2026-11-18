from psycopg_pool import AsyncConnectionPool

from curio.settings import Settings


def create_pool(settings: Settings) -> AsyncConnectionPool:
    """Build an unopened pool. The caller opens and closes it (app lifespan or worker)."""
    return AsyncConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_pool_timeout_seconds,
        open=False,
    )
