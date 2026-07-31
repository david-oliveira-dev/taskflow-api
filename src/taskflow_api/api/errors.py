"""Translating domain errors into HTTP responses.

Registered once on the application rather than caught in every handler. Nine routers each
wrapping their service call in the same `try/except NotFoundError` is nine chances to
forget one, and a forgotten one is a 500 where a 404 belonged.

The response body matches what `HTTPException` produces, so a client sees one shape whether
the failure came from a dependency or from a service. Phase 4 replaces both with the
standard envelope and its correlation id; the status mapping below is what that envelope
will wrap, and does not change.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from taskflow_api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    BusinessRuleError,
    ConflictError,
    InvalidCursorError,
    NotFoundError,
    TaskFlowError,
)

#: Ordered most specific first — the first matching class wins, so a subclass added later
#: cannot be swallowed by its base.
_STATUS_BY_ERROR: tuple[tuple[type[TaskFlowError], int], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ConflictError, status.HTTP_409_CONFLICT),
    (BusinessRuleError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InvalidCursorError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (AuthorizationError, status.HTTP_403_FORBIDDEN),
    (AuthenticationError, status.HTTP_401_UNAUTHORIZED),
)


def status_for(error: TaskFlowError) -> int:
    """Map a domain error to the status that describes it.

    Args:
        error: The raised domain error.

    Returns:
        The HTTP status code. Anything unmapped is a 500 on purpose — an error the edge does
        not recognise is a bug in this service, not a way for the client to fix its request.
    """
    for error_type, code in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return code
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def install_error_handlers(app: FastAPI) -> None:
    """Register the domain-error handler on an application."""

    @app.exception_handler(TaskFlowError)
    async def _handle(request: Request, exc: Exception) -> JSONResponse:
        # Narrowed by the registration above; the signature is what Starlette requires.
        assert isinstance(exc, TaskFlowError)
        code = status_for(exc)
        if code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            # Never echo the message of an error we failed to classify: it was written for
            # a log, not for a stranger.
            raise exc
        return JSONResponse(status_code=code, content={"detail": str(exc)})
