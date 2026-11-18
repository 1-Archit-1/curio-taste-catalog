"""The two tests run in file order: the second checks the first's write was rolled back."""


async def test_write_inside_test_transaction(conn):
    await conn.execute("CREATE TABLE rollback_probe (id int)")
    await conn.execute("INSERT INTO rollback_probe VALUES (1)")


async def test_previous_write_was_rolled_back(conn):
    cur = await conn.execute("SELECT to_regclass('rollback_probe')")
    assert (await cur.fetchone()) == (None,)
