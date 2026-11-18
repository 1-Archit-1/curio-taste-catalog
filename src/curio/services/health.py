from psycopg import AsyncConnection

from curio.repositories import health as health_repo


async def database_ok(conn: AsyncConnection) -> bool:
    return await health_repo.ping(conn)
