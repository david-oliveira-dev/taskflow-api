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
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from taskflow_api.config import Settings
from taskflow_api.db.redis import create_redis
from taskflow_api.db.session import create_session_factory
from taskflow_api.main import create_app
from taskflow_api.services.idempotency import IdempotencyStore
from taskflow_api.services.ratelimit import RateLimiter

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


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """Start Redis 7 and yield its URL.

    Started per session like Postgres: on a mechanical disk the container start dominates
    everything else, and each test uses its own key prefix rather than its own server.
    """
    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


#: Effectively unlimited, so ordinary tests never meet the limiter.
#:
#: Not a workaround for a broken limiter — the limiter is right, and that is the problem.
#: Requests made through the ASGI transport carry no peer address, so every unauthenticated
#: call in the suite (every register, every login) counts against the one `ip:unknown`
#: bucket. At the production default of 120/minute the suite throttles itself, and the
#: failure lands on whichever test happens to run at the wrong moment rather than on
#: anything real.
#:
#: The limiter is exercised properly in `test_ratelimit_api.py`, which builds its own app
#: with a small limit. Keeping that opt-in means changing the default can never quietly
#: start breaking unrelated tests.
UNTHROTTLED = 1_000_000


@pytest.fixture
def app_settings(migrated_dsn: str, redis_url: str) -> Settings:
    """Settings pointed at both throwaway containers, with the limiter effectively off."""
    return Settings(
        _env_file=None,
        environment="local",
        database_url=migrated_dsn,
        redis_url=redis_url,
        jwt_secret="an-integration-test-key-long-enough-for-hs256",
        rate_limit_requests=UNTHROTTLED,
    )


def build_app(settings: Settings) -> tuple[FastAPI, AsyncEngine, Redis]:
    """Wire an app to the test containers, bypassing its own lifespan.

    The lifespan would build an engine and a client from the configured DSNs, which is
    exactly right in production and wrong here — the fixtures own those. Everything
    downstream of `app.state` behaves identically either way.
    """
    built = create_app(settings)
    engine = create_async_engine(str(settings.database_url))
    redis = create_redis(settings)
    session_factory = create_session_factory(engine)

    built.state.settings = settings
    built.state.engine = engine
    built.state.redis = redis
    built.state.session_factory = session_factory
    built.state.idempotency = IdempotencyStore(session_factory)
    built.state.rate_limiter = RateLimiter(
        redis,
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    return built, engine, redis


@pytest.fixture
async def app(app_settings: Settings) -> AsyncIterator[FastAPI]:
    """An app wired to the containerised database and Redis."""
    built, engine, redis = build_app(app_settings)

    yield built

    await engine.dispose()
    await redis.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app in-process, with no socket involved."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
