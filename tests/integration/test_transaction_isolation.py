"""The `conn` fixture's writes stay inside an uncommitted transaction.

Another connection can't see them, so the fixture's rollback at teardown
leaves nothing behind. Doesn't depend on test order.
"""

import psycopg


async def test_fixture_writes_are_not_committed(conn, migrated_database_url):
    await conn.execute("CREATE TABLE rollback_probe (id int)")
    await conn.execute("INSERT INTO rollback_probe VALUES (1)")

    async with await psycopg.AsyncConnection.connect(migrated_database_url) as other:
        cur = await other.execute("SELECT to_regclass('rollback_probe')")
        assert (await cur.fetchone()) == (None,)
