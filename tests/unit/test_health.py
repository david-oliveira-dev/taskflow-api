"""Liveness endpoint and the app factory."""

from httpx import AsyncClient

from taskflow_api import __version__
from taskflow_api.config import Settings
from taskflow_api.main import create_app


async def test_health_reports_ok_and_version(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_docs_are_exposed_outside_production(settings: Settings) -> None:
    app = create_app(settings)

    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"


async def test_docs_are_disabled_in_production(settings: Settings) -> None:
    """Interactive docs must not be reachable in production."""
    production = settings.model_copy(update={"environment": "production"})

    app = create_app(production)

    assert app.docs_url is None
    assert app.openapi_url is None
