"""A route class that commits before the client is told the write succeeded.

FastAPI closes dependencies declared with `yield` from an `AsyncExitStack` that Starlette
unwinds **after** the response bytes have gone out. A session that commits in that teardown
therefore acknowledges a write before it is durable, and the window is wide enough to lose:

    POST /auth/register  -> 201
    POST /auth/login     -> 401   (the account is not visible yet)

Measured on this service before the fix: ten out of ten immediate logins failed, and the
same credentials worked a second later.

In-process tests cannot see this. `httpx`'s ASGI transport awaits the whole application
call, exit-stack teardown included, so by the time a test reads the response the commit has
always landed. Only a client on a real socket observes the gap — which is why
`tests/integration/test_read_after_write.py` runs a live server.

Committing here, after the endpoint has produced its `Response` object but before Starlette
sends it, closes the window without moving the transaction boundary: it is still one
transaction per request, owned by the edge.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession


class TransactionalRoute(APIRoute):
    """Route whose request transaction commits before the response is sent."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the generated handler with the commit."""
        handle = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response = await handle(request)
            # Absent on routes that never asked for a session, such as the liveness probe.
            session: AsyncSession | None = getattr(request.state, "session", None)
            if session is not None:
                await session.commit()
            return response

        return handler
