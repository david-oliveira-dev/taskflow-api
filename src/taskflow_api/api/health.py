"""Liveness and readiness endpoints.

The two are deliberately different, and conflating them is a real operational bug:

- `/health` answers "is this process alive?" and must never touch Postgres or Redis. If it
  did, a database blip would make the orchestrator kill and restart healthy pods, turning a
  dependency outage into an outage of everything.
- `/ready` answers "can this process serve traffic right now?" and *does* check
  dependencies, so the load balancer stops sending requests it cannot fulfil.

`/ready` is wired to real checks in Phase 4, once Postgres and Redis have sessions.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from taskflow_api import __version__

router = APIRouter(tags=["ops"])


class HealthResponse(BaseModel):
    """Payload of the liveness probe."""

    status: Literal["ok"]
    version: str


@router.get("/health", summary="Liveness probe")
async def health() -> HealthResponse:
    """Report that the process is up, without touching any dependency."""
    return HealthResponse(status="ok", version=__version__)
