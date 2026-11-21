from sqlalchemy.ext.asyncio import AsyncSession

from curio.repositories import health as health_repo


async def database_ok(session: AsyncSession) -> bool:
    return await health_repo.ping(session)
