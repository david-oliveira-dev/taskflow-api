"""Application factory and ASGI entry point."""

from fastapi import FastAPI

from taskflow_api import __version__
from taskflow_api.api.health import router as health_router
from taskflow_api.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Written as a factory rather than a module-level singleton so tests can build an app
    against throwaway settings without mutating global state or re-importing the module.

    Args:
        settings: Overrides the environment-derived configuration. Tests pass this.

    Returns:
        A configured FastAPI application.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="TaskFlow API",
        version=__version__,
        summary=(
            "Projects and tasks over a layered service, used to demonstrate audit trails, "
            "idempotency and rate limiting."
        ),
        # Interactive docs are a foot-gun in production and a feature everywhere else.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.include_router(health_router)
    return app
