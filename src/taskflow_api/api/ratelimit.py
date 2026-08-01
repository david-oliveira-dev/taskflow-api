"""The rate-limit middleware.

Runs before routing, so the quota is spent before any handler or database work happens —
which is the only ordering that protects anything.

That placement forces one compromise worth naming. The middleware needs an identity, but
authentication is a dependency and has not run yet. Rather than duplicate the whole auth
path, it reads the `sub` claim from the bearer token **without verifying the signature**,
falling back to the client address. That is safe here precisely because the identity is only
used to pick a counter: forging a token buys an attacker a different bucket, never access.
The real signature check still happens in `get_current_user`, before anything is served.
"""

import logging
from collections.abc import Awaitable, Callable

import jwt
from fastapi import Request, Response, status

from taskflow_api.api.errors import error_response
from taskflow_api.services.ratelimit import Decision, RateLimiter

logger = logging.getLogger(__name__)

#: Paths that must answer even when a caller is over quota. A liveness probe throttled by
#: the traffic it is meant to survive gets the container killed during an incident.
EXEMPT_PATHS = frozenset({"/health", "/ready"})


def identify(request: Request) -> str:
    """Pick the bucket this request counts against.

    Prefers the account named by the bearer token so that one noisy client cannot exhaust
    the quota of everyone behind the same address. Falls back to the peer address, which is
    all an unauthenticated caller has.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            pass
        else:
            subject = claims.get("sub")
            if isinstance(subject, str) and subject:
                return f"user:{subject}"

    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def _headers(decision: Decision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after),
    }


async def rate_limit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Spend one unit of quota, or refuse with 429.

    The three `X-RateLimit-*` headers go on **every** response, not just rejections: a
    client can only back off before hitting the wall if it is told how close it is.
    """
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None or request.url.path in EXEMPT_PATHS:
        return await call_next(request)

    decision = await limiter.check(identify(request))

    if not decision.allowed:
        return error_response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="rate_limited",
            message="Too many requests. Slow down and retry later.",
            headers={**_headers(decision), "Retry-After": str(decision.reset_after)},
        )

    response = await call_next(request)
    response.headers.update(_headers(decision))
    if decision.degraded:
        # Says out loud that the quota was not actually enforced for this request.
        response.headers["X-RateLimit-Degraded"] = "true"
    return response
