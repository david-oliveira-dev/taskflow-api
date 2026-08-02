"""Structured logging.

Logs are JSON in `ci` and `production`, and human-readable when running locally. That is
the only environment-dependent behaviour here: a developer reading a terminal and a log
shipper parsing a stream want opposite things, and forcing either one to accommodate the
other costs more than the branch does.

Three properties are the point:

- **Every line carries the correlation id.** Injected by a processor that reads the context
  variable, so no call site has to remember it — and the one place a request's lines can be
  gathered from is the same token the client was handed in the response.
- **Standard-library logs go through the same pipeline.** SQLAlchemy, uvicorn and anything
  else using `logging` end up in the same format. Half a log stream in JSON and half in
  prose is a stream nothing can parse.
- **Nothing sensitive is bound.** The access line records method, path, status and
  duration. Not the query string, not headers, not the body: the first carries tokens in
  the wild, the second carries `Authorization`, and the third carries passwords.
"""

import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import Request, Response

from taskflow_api.api.correlation import correlation_id
from taskflow_api.config import Settings

access_logger = structlog.get_logger("taskflow.access")


def add_correlation_id(_logger: object, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    """Attach the current request's id to a log line, when there is one.

    A processor rather than a `bind()` at the edge: this way a log line written deep inside
    a service is correlated without that service knowing a request exists.
    """
    request_id = correlation_id()
    if request_id:
        event["correlation_id"] = request_id
    return event


def configure_logging(settings: Settings) -> None:
    """Set up structlog and route the standard library through it.

    Idempotent, so building a second application in the same process — which the tests do
    constantly — does not stack processors or duplicate handlers.

    Args:
        settings: Source of the log level and of whether output should be JSON.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_correlation_id,
        timestamper,
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # `foreign_pre_chain` is what makes a plain `logging.warning()` from SQLAlchemy
            # come out with the same fields as a structlog call.
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # uvicorn installs its own handlers and its own access log. Left alone, every request
    # would be reported twice, once in each format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


async def access_log_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Log one line per request, with its outcome and how long it took.

    Deliberately narrow. `request.url.path` rather than the full URL, because query strings
    end up holding tokens in the wild; no headers, because `Authorization` is one; no body,
    because that is where passwords are.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        access_logger.exception(
            "request failed",
            method=request.method,
            path=request.url.path,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        raise

    access_logger.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return response
