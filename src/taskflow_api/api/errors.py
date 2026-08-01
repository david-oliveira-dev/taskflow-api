"""One error shape, for every failure the service can produce.

    {"error": {"code": ..., "message": ..., "detail": ...}, "correlation_id": ...}

Registered as application-wide handlers rather than caught in each route. Nine routers each
wrapping their service call in the same `try/except` is nine chances to forget one, and a
forgotten one is a 500 where a 404 belonged.

Three properties are deliberate:

- **`code` is stable and machine-readable.** `message` is prose and may be reworded; a
  client that branches on wording breaks when someone fixes a typo.
- **Unhandled exceptions never reach the client.** They become `internal_error` with a fixed
  message. An unrecognised failure is this service's bug, and its text was written for a log
  — it can name tables, columns, or a fragment of a query.
- **Every response carries the correlation id**, so a user reporting "it failed" hands over
  the one token that finds the request in the logs.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from taskflow_api.api.correlation import correlation_id
from taskflow_api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    BusinessRuleError,
    ConflictError,
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    InvalidCursorError,
    NotFoundError,
    TaskFlowError,
)

#: Ordered most specific first — the first matching class wins, so a subclass added later
#: cannot be swallowed by its base.
_BY_ERROR: tuple[tuple[type[TaskFlowError], int, str], ...] = (
    (IdempotencyInProgressError, status.HTTP_409_CONFLICT, "idempotency_key_in_progress"),
    (IdempotencyKeyReusedError, status.HTTP_422_UNPROCESSABLE_CONTENT, "idempotency_key_reused"),
    (NotFoundError, status.HTTP_404_NOT_FOUND, "not_found"),
    (ConflictError, status.HTTP_409_CONFLICT, "conflict"),
    (BusinessRuleError, status.HTTP_422_UNPROCESSABLE_CONTENT, "unprocessable"),
    (InvalidCursorError, status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_cursor"),
    (AuthorizationError, status.HTTP_403_FORBIDDEN, "forbidden"),
    (AuthenticationError, status.HTTP_401_UNAUTHORIZED, "unauthenticated"),
)

#: Codes for the statuses raised as `HTTPException` by dependencies and routers.
_BY_STATUS: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthenticated",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "unprocessable",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_503_SERVICE_UNAVAILABLE: "unavailable",
}

INTERNAL_ERROR_MESSAGE = "The service failed to handle this request."


def classify(error: TaskFlowError) -> tuple[int, str]:
    """Map a domain error to the status and code that describe it.

    Args:
        error: The raised domain error.

    Returns:
        The HTTP status and the stable error code. Anything unmapped is a 500 on purpose —
        an error the edge does not recognise is a bug here, not something the client can fix
        by changing its request.
    """
    for error_type, code, slug in _BY_ERROR:
        if isinstance(error, error_type):
            return code, slug
    return status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    detail: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the standard envelope.

    Args:
        status_code: The HTTP status.
        code: Stable, machine-readable identifier for the failure.
        message: Human-readable summary. Safe to reword; clients must not branch on it.
        detail: Optional structured context, such as per-field validation errors.
        headers: Extra response headers, e.g. `Retry-After` or `WWW-Authenticate`.

    Returns:
        The response.
    """
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {"code": code, "message": message, "detail": detail},
            "correlation_id": correlation_id(),
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register every error handler on an application."""

    @app.exception_handler(TaskFlowError)
    async def _domain(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, TaskFlowError)
        status_code, code = classify(exc)
        if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            # Never echo the message of an error the edge failed to classify.
            return error_response(
                status_code=status_code, code=code, message=INTERNAL_ERROR_MESSAGE
            )
        return error_response(status_code=status_code, code=code, message=str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        return error_response(
            status_code=exc.status_code,
            code=_BY_STATUS.get(exc.status_code, "error"),
            message=str(exc.detail),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="validation_error",
            message="The request body or parameters are not valid.",
            # Pydantic's own per-field report, minus the `input` it echoes back: a rejected
            # password would otherwise be reflected into the response and the logs.
            detail=[
                {key: value for key, value in item.items() if key not in {"input", "ctx", "url"}}
                for item in exc.errors()
            ],
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Starlette re-raises after this in debug/test configurations, which is what keeps
        # the traceback visible where it is wanted. The client only ever sees the envelope.
        return error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message=INTERNAL_ERROR_MESSAGE,
        )
