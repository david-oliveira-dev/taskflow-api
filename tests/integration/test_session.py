"""The transaction boundary.

`session_scope` is where "the whole request succeeded" is decided. It is worth its own
tests because the failure it guards against is invisible in unit tests: a service that
raises halfway through leaving half its writes committed.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from taskflow_api.db.models import User
from taskflow_api.db.session import create_session_factory, session_scope

pytestmark = pytest.mark.integration


def _user(email: str) -> User:
    return User(email=email, hashed_password="hashed", full_name="Ada Lovelace")


async def test_work_is_committed_when_the_block_succeeds(migrated_dsn: str) -> None:
    engine = create_async_engine(migrated_dsn)
    factory = create_session_factory(engine)
    email = f"{uuid.uuid4()}@example.com"

    async for session in session_scope(factory):
        session.add(_user(email))

    async with factory() as check:
        found = await check.execute(select(User).where(User.email == email))
        assert found.scalar_one_or_none() is not None

    await engine.dispose()


async def test_nothing_is_committed_when_the_block_raises(migrated_dsn: str) -> None:
    """The guarantee that matters: a partial write must not survive.

    Without the rollback, the user below would be committed even though the operation as a
    whole failed — the classic way an audit entry goes missing while the change it was
    meant to record does not.
    """
    engine = create_async_engine(migrated_dsn)
    factory = create_session_factory(engine)
    email = f"{uuid.uuid4()}@example.com"

    with pytest.raises(RuntimeError, match="boom"):
        async for session in session_scope(factory):
            session.add(_user(email))
            await session.flush()
            raise RuntimeError("boom")

    async with factory() as check:
        found = await check.execute(select(User).where(User.email == email))
        assert found.scalar_one_or_none() is None, "a failed unit of work left data behind"

    await engine.dispose()
