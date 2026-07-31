"""Application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from taskflow_api import __version__
from taskflow_api.api.audit import router as audit_router
from taskflow_api.api.auth import router as auth_router
from taskflow_api.api.errors import install_error_handlers
from taskflow_api.api.health import router as health_router
from taskflow_api.api.projects import router as projects_router
from taskflow_api.api.tasks import router as tasks_router
from taskflow_api.config import Settings, get_settings
from taskflow_api.db.session import create_engine, create_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Written as a factory rather than a module-level singleton so tests can build an app
    against throwaway settings without mutating global state or re-importing the module.

    Args:
        settings: Overrides the environment-derived configuration. Tests pass this.

    Returns:
        A configured FastAPI application.
    """
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the engine for the life of the process.

        One engine, one connection pool, created at startup and disposed at shutdown.
        Creating an engine per request would open a new pool per request, which exhausts
        the database's connection limit under any real load.
        """
        engine = create_engine(resolved)
        app.state.settings = resolved
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="TaskFlow API",
        version=__version__,
        summary=(
            "Projects and tasks over a layered service, used to demonstrate audit trails, "
            "idempotency and rate limiting."
        ),
        lifespan=lifespan,
        # Interactive docs are a foot-gun in production and a feature everywhere else.
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
    )

    install_error_handlers(app)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(tasks_router)
    app.include_router(audit_router)
    return app
