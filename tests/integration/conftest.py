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
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from taskflow_api.config import Settings
from taskflow_api.db.session import create_session_factory
from taskflow_api.main import create_app

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


@pytest.fixture
async def app(migrated_dsn: str) -> AsyncIterator[FastAPI]:
    """An app wired to the containerised database.

    The app's own lifespan is bypassed so the engine can be pointed at the test container;
    everything downstream of `app.state` behaves exactly as it does in production.
    """
    settings = Settings(
        _env_file=None,
        environment="local",
        database_url=migrated_dsn,
        redis_url="redis://localhost:6379/0",
        jwt_secret="an-integration-test-key-long-enough-for-hs256",
    )
    built = create_app(settings)
    engine = create_async_engine(migrated_dsn)
    built.state.settings = settings
    built.state.engine = engine
    built.state.session_factory = create_session_factory(engine)

    yield built

    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app in-process, with no socket involved."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
