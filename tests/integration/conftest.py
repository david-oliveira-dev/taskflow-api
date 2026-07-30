"""Fixtures for tests that need a real PostgreSQL.

The schema is created by running **the actual Alembic migration**, not
`Base.metadata.create_all`. That choice matters: `create_all` builds the schema from the
models, so the migration could be broken or drifted and every test would still pass. Running
the migration means each integration run also proves that `upgrade head` works.

The container is started once per session — on a mechanical disk, starting it per test would
dominate the runtime.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Start PostgreSQL 16 and yield an async DSN pointing at it."""
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def migrated_dsn(postgres_dsn: str) -> str:
    """Apply every migration to the throwaway database.

    The DSN is handed to Alembic through its own config rather than through the
    environment. Setting `DATABASE_URL` here would outlive this fixture and leak into the
    unit tests, where "does a missing setting fail?" would then quietly pass for the wrong
    reason. Environment mutation in a session-scoped fixture is a trap.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", postgres_dsn)
    command.upgrade(config, "head")
    return postgres_dsn


@pytest.fixture
async def session(migrated_dsn: str) -> AsyncIterator[AsyncSession]:
    """Yield a session whose work is always rolled back.

    Each test runs inside a transaction that is discarded at the end, so tests share one
    container without sharing state and without paying to recreate the schema.
    """
    engine = create_async_engine(migrated_dsn, poolclass=None)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False)

    async with factory() as db_session:
        yield db_session

    await transaction.rollback()
    await connection.close()
    await engine.dispose()
