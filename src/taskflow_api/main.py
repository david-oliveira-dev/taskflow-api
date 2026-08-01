"""Application factory and ASGI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from taskflow_api import __version__
from taskflow_api.api.audit import router as audit_router
from taskflow_api.api.auth import router as auth_router
from taskflow_api.api.correlation import CorrelationIdMiddleware
from taskflow_api.api.errors import install_error_handlers
from taskflow_api.api.health import router as health_router
from taskflow_api.api.idempotency import install_replay_handler
from taskflow_api.api.projects import router as projects_router
from taskflow_api.api.ratelimit import rate_limit_middleware
from taskflow_api.api.tasks import router as tasks_router
from taskflow_api.config import Settings, get_settings
from taskflow_api.db.redis import create_redis
from taskflow_api.db.session import create_engine, create_session_factory
from taskflow_api.services.idempotency import IdempotencyStore
from taskflow_api.services.ratelimit import RateLimiter


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
        """Own the engine and the Redis client for the life of the process.

        One engine, one connection pool, created at startup and disposed at shutdown.
        Creating either per request would open a connection per request, which exhausts
        the server's limit under any real load.
        """
        engine = create_engine(resolved)
        redis = create_redis(resolved)
        session_factory = create_session_factory(engine)

        app.state.settings = resolved
        app.state.engine = engine
        app.state.redis = redis
        app.state.session_factory = session_factory
        app.state.idempotency = IdempotencyStore(session_factory)
        app.state.rate_limiter = RateLimiter(
            redis,
            limit=resolved.rate_limit_requests,
            window_seconds=resolved.rate_limit_window_seconds,
        )
        try:
            yield
        finally:
            await engine.dispose()
            await redis.aclose()

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
    install_replay_handler(app)

    # Order matters, and reads inside-out: the correlation id is added last so it is the
    # outermost middleware, which means a request rejected by the rate limiter still
    # carries an id a user can quote.
    app.middleware("http")(rate_limit_middleware)
    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(tasks_router)
    app.include_router(audit_router)
    return app
