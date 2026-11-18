from psycopg import AsyncConnection


async def ping(conn: AsyncConnection) -> bool:
    cur = await conn.execute("SELECT 1")
    row = await cur.fetchone()
    return row == (1,)
