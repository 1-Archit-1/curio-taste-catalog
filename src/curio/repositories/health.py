from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def ping(session: AsyncSession) -> bool:
    result = await session.execute(select(1))
    return result.scalar_one() == 1
