"""The Redis connection pool.

Redis holds only rate-limit counters here, which is why the compose file runs it without
persistence: every key has a short TTL, and losing the lot on restart costs at most one
window of leniency. Nothing durable is ever written to it.
"""

from redis.asyncio import Redis

from taskflow_api.config import Settings


def create_redis(settings: Settings) -> Redis:
    """Build the client.

    One client per process, owned by the application lifespan, for the same reason as the
    database engine: it carries a connection pool, and building one per request would open a
    connection per request.

    Timeouts are short and explicit. The rate limiter fails open, so a Redis that hangs must
    turn into a fast error rather than a slow one — an unbounded wait here would convert
    "the limiter is down" into "the API is down", which is exactly the outcome failing open
    exists to prevent.

    Args:
        settings: Source of the Redis DSN.

    Returns:
        A configured async client.
    """
    return Redis.from_url(
        str(settings.redis_url),
        decode_responses=True,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
        health_check_interval=30,
    )
