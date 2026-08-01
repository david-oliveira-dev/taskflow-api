"""Rate limiting: a sliding window, decided atomically inside Redis.

Two properties matter more than the algorithm, and both are easy to get wrong.

**Atomicity.** `GET`, then decide in Python, then `SET` is a race: under concurrency several
callers read the same count and all conclude they are under the limit. A limiter with a race
is worse than no limiter, because it reports a guarantee it does not provide. The decision
therefore happens in one Lua script, which Redis runs to completion without interleaving.

**Failure mode: open.** If Redis is unreachable the request is allowed and a warning is
logged. Rate limiting protects against excess load; it is not an authorisation control, and
letting its outage take the whole API down trades a small problem for a total one. The
opposite choice — fail closed — is defensible where the limiter guards something like fraud,
and this is not that.

The window is a sorted set of request timestamps, trimmed on every call. A fixed window done
with `INCR`/`EXPIRE` is cheaper, but lets a caller send the full quota at the end of one
window and again at the start of the next — twice the intended rate across the boundary.
"""

import logging
import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# KEYS[1] = window key
# ARGV = now (float seconds), window seconds, limit, unique member for this request
#
# Returns {allowed, remaining, reset_after}. The trim happens before the count, so entries
# that have aged out of the window never affect the decision.
_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local used = redis.call('ZCARD', key)

if used < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, math.ceil(window))
    return {1, limit - used - 1, window}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry = window
if oldest[2] then
    retry = (tonumber(oldest[2]) + window) - now
    if retry < 0 then retry = 0 end
end
return {0, 0, retry}
"""


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of one rate-limit check."""

    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the window frees a slot. Becomes `Retry-After` on a 429.
    reset_after: int
    #: True when Redis could not be reached and the request was let through anyway.
    degraded: bool = False


class RateLimiter:
    """Counts requests per identity over a sliding window."""

    def __init__(self, redis: Redis, *, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds
        self._script = redis.register_script(_SLIDING_WINDOW)

    async def check(self, identity: str) -> Decision:
        """Record a request against `identity` and say whether it may proceed.

        Args:
            identity: What the quota is counted against — an account id where the caller is
                authenticated, otherwise a client address.

        Returns:
            The decision. Never raises on a Redis failure: that path returns `allowed` with
            `degraded` set, which is the fail-open choice made explicit rather than implied
            by a swallowed exception.
        """
        now = time.time()
        try:
            allowed, remaining, reset_after = await self._script(
                keys=[f"ratelimit:{identity}"],
                args=[now, self._window, self._limit, uuid.uuid4().hex],
            )
        except RedisError:
            logger.warning("rate limiter unavailable, allowing request (fail-open)", exc_info=True)
            return Decision(
                allowed=True,
                limit=self._limit,
                remaining=self._limit,
                reset_after=self._window,
                degraded=True,
            )

        return Decision(
            allowed=bool(allowed),
            limit=self._limit,
            remaining=int(remaining),
            # Ceiling, never zero on a rejection: `Retry-After: 0` invites an immediate
            # retry that is certain to be rejected again.
            reset_after=max(1, int(-(-float(reset_after) // 1))),
        )
