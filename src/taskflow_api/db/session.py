"""Async engine and session management."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from taskflow_api.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine.

    Args:
        settings: Source of the database DSN.

    Returns:
        An engine with a pool sized for a single service instance.
    """
    return create_async_engine(
        str(settings.database_url),
        # Off by default: SQL echoed at INFO would put query text, and therefore user data,
        # into the log stream. Turn it on deliberately while debugging, never in production.
        echo=False,
        pool_size=5,
        max_overflow=10,
        # Recycle before a proxy or the server silently drops an idle connection, which
        # otherwise surfaces as a random "connection was closed" on an unlucky request.
        pool_recycle=1800,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to `engine`.

    `expire_on_commit` is off so that attributes stay readable after a commit; with it on,
    returning an object from a service that has just committed triggers a lazy refresh,
    which in async code raises instead of quietly issuing a query.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session that commits on success and rolls back on failure.

    The transaction boundary lives here, at the edge, rather than inside repositories.
    A repository that commits on its own makes it impossible for a service to write two
    entities atomically — which is exactly what "create the task *and* its audit entry"
    needs.
    """
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
