"""The `session` fixture's writes stay inside an uncommitted outer transaction.

Another session can't see them, so the fixture's rollback at teardown
leaves nothing behind. Doesn't depend on test order.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from curio.repositories import users as users_repo


async def test_fixture_writes_are_not_committed(
    session: AsyncSession,
    audit_session_factory: async_sessionmaker[AsyncSession],
):
    await users_repo.create_user(
        session,
        username="rollback_probe_user",
        addon_token="rollback_probe_token",
    )
    await session.flush()

    async with audit_session_factory() as other:
        found = await users_repo.get_user_by_username(other, username="rollback_probe_user")
        assert found is None
