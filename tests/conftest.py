"""Shared fixtures.

Unit tests here never touch Postgres or Redis: settings are built in-process with dummy
DSNs, so the suite stays offline and deterministic (general standard, section 6).
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from taskflow_api.config import Settings
from taskflow_api.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Settings with placeholder values, valid enough to build the app."""
    return Settings(
        environment="local",
        database_url="postgresql+asyncpg://user:pass@localhost:5432/taskflow",
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-not-used-for-signing-yet",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the app in-process, with no socket involved."""
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
