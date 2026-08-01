"""Liveness and readiness endpoints.

The two are deliberately different, and conflating them is a real operational bug:

- `/health` answers "is this process alive?" and must never touch Postgres or Redis. If it
  did, a database blip would make the orchestrator kill and restart healthy pods, turning a
  dependency outage into an outage of everything.
- `/ready` answers "can this process serve traffic right now?" and *does* check
  dependencies, so the load balancer stops sending requests it cannot fulfil.

Both are exempt from rate limiting. A probe throttled by the very traffic it exists to
survive gets the container killed in the middle of an incident.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskflow_api import __version__

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


class HealthResponse(BaseModel):
    """Payload of the liveness probe."""

    status: Literal["ok"]
    version: str


class ReadyResponse(BaseModel):
    """Payload of the readiness probe, naming each dependency separately.

    Reporting per-dependency state rather than one boolean is what makes the endpoint
    useful during an incident: "not ready" sends someone to read logs, while
    `{"database": "ok", "redis": "unavailable"}` says where to look.
    """

    status: Literal["ready", "degraded"]
    database: Literal["ok", "unavailable"]
    redis: Literal["ok", "unavailable"]


@router.get("/health", summary="Liveness probe")
async def health() -> HealthResponse:
    """Report that the process is up, without touching any dependency."""
    return HealthResponse(status="ok", version=__version__)


@router.get(
    "/ready",
    summary="Readiness probe",
    responses={503: {"description": "A required dependency is unreachable."}},
)
async def ready(request: Request, response: Response) -> ReadyResponse:
    """Check that Postgres and Redis are actually reachable.

    Postgres failing means nothing can be served, so this answers **503** and the load
    balancer takes the instance out of rotation.

    Redis failing does **not**. The rate limiter fails open, so the API still works, with
    its quota unenforced — degraded, not unusable. Answering 503 there would take down a
    working service to punish the loss of a throttle. Either way the body says which one.
    """
    database = await _check_database(request.app.state.session_factory)
    cache = await _check_redis(getattr(request.app.state, "redis", None))

    if database != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        status="ready" if database == "ok" and cache == "ok" else "degraded",
        database=database,
        redis=cache,
    )


async def _check_database(
    factory: async_sessionmaker[AsyncSession],
) -> Literal["ok", "unavailable"]:
    """Round-trip the cheapest possible query.

    `SELECT 1` proves the pool can still hand out a live connection, which is the thing
    that breaks. Anything heavier would make the probe a source of load in its own right.
    """
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        # OSError as well as SQLAlchemyError: asyncpg lets a refused connection propagate
        # raw, so catching only the ORM's own class turns "the database is down" into a
        # 500 from the readiness probe itself.
        logger.warning("readiness: database unreachable", exc_info=True)
        return "unavailable"
    return "ok"


async def _check_redis(client: Redis | None) -> Literal["ok", "unavailable"]:
    """Ping Redis, treating an unconfigured client as unavailable."""
    if client is None:  # pragma: no cover - only before the lifespan has run
        return "unavailable"
    try:
        await client.ping()
    except (RedisError, OSError):
        logger.warning("readiness: redis unreachable", exc_info=True)
        return "unavailable"
    return "ok"
