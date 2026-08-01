"""One id per request, carried through the response and available to every layer.

The id lives in a `ContextVar` rather than being threaded through call signatures, because
the layers that most want it — an error handler, a log line inside a service — are exactly
the ones with no reason to accept a `Request`.

An inbound `X-Correlation-Id` is honoured so a trace survives across services, but it is
validated first. The value ends up in log lines and in response bodies; accepting arbitrary
client input there is how log forging and header injection start.
"""

import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER_NAME = "x-correlation-id"

#: Conservative on purpose: printable, bounded, no separators that could split a log line.
_ACCEPTABLE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def correlation_id() -> str:
    """The current request's id, or an empty string outside a request."""
    return _correlation_id.get()


def _resolve(supplied: str | None) -> str:
    """Take the caller's id if it is safe to echo, otherwise mint one."""
    if supplied is not None and _ACCEPTABLE.match(supplied):
        return supplied
    return str(uuid.uuid4())


class CorrelationIdMiddleware:
    """Assigns an id to every request and returns it on the response.

    Written as raw ASGI rather than `BaseHTTPMiddleware`: the latter runs the endpoint in a
    separate task, which means the `ContextVar` set here would not be visible to the code
    that needs it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve(Headers(scope=scope).get(HEADER_NAME))
        token = _correlation_id.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.append((HEADER_NAME.encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _correlation_id.reset(token)
