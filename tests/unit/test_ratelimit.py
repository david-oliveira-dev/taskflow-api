"""Rate limiter behaviour that needs no Redis.

The Lua script itself is exercised against a real server in the integration tests — a fake
Redis would prove only that the fake agrees with itself, and the whole point of the script
is that *Redis* evaluates it atomically.
"""

from typing import Any

import jwt
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from taskflow_api.api.ratelimit import identify
from taskflow_api.services.ratelimit import RateLimiter

# Long enough to keep PyJWT from warning about HMAC key length; the values are irrelevant
# here, since `identify` never verifies a signature.
_A_KEY_LONG_ENOUGH_TO_NOT_WARN = "x" * 32
_A_DIFFERENT_KEY_LONG_ENOUGH = "y" * 32


class _Script:
    """Stands in for the registered Lua script."""

    def __init__(self, result: list[int] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *, keys: list[str], args: list[Any]) -> list[int]:
        self.calls.append({"keys": keys, "args": args})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _limiter(result: list[int] | Exception) -> tuple[RateLimiter, _Script]:
    """A limiter whose Lua call is replaced by a recording double."""
    script = _Script(result)

    class _Redis:
        def register_script(self, _: str) -> _Script:
            return script

    return RateLimiter(_Redis(), limit=10, window_seconds=60), script  # type: ignore[arg-type]


class TestDecision:
    async def test_an_allowed_request_reports_what_is_left(self) -> None:
        limiter, _ = _limiter([1, 7, 60])

        decision = await limiter.check("user:abc")

        assert decision.allowed is True
        assert (decision.limit, decision.remaining) == (10, 7)
        assert decision.degraded is False

    async def test_a_rejected_request_never_says_retry_after_zero(self) -> None:
        """`Retry-After: 0` invites an immediate retry that is certain to fail again."""
        limiter, _ = _limiter([0, 0, 0])

        decision = await limiter.check("user:abc")

        assert decision.allowed is False
        assert decision.reset_after >= 1

    async def test_a_fractional_wait_rounds_up(self) -> None:
        """Rounding down would let the caller retry just before a slot frees."""
        limiter, _ = _limiter([0, 0, 4.2])  # type: ignore[list-item]

        assert (await limiter.check("user:abc")).reset_after == 5

    async def test_the_counter_is_scoped_by_identity(self) -> None:
        limiter, script = _limiter([1, 9, 60])

        await limiter.check("user:abc")

        assert script.calls[0]["keys"] == ["ratelimit:user:abc"]


class TestFailOpen:
    async def test_an_unreachable_redis_lets_the_request_through(self) -> None:
        """The deliberate trade-off: losing the throttle must not lose the API.

        Fail-closed is defensible where a limiter guards fraud. Here it would turn the loss
        of a counter into a total outage.
        """
        limiter, _ = _limiter(RedisConnectionError("down"))

        decision = await limiter.check("user:abc")

        assert decision.allowed is True
        assert decision.degraded is True

    async def test_the_failure_is_logged_as_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failing open silently would hide that the quota stopped being enforced."""
        limiter, _ = _limiter(RedisConnectionError("down"))

        with caplog.at_level("WARNING"):
            await limiter.check("user:abc")

        assert any(record.levelname == "WARNING" for record in caplog.records)


class TestIdentify:
    def _request(self, *, header: str | None = None, host: str | None = "203.0.113.7") -> Any:
        class _Client:
            def __init__(self, host: str) -> None:
                self.host = host

        class _Request:
            def __init__(self) -> None:
                self.headers = {"authorization": header} if header else {}
                self.client = _Client(host) if host else None

        return _Request()

    def test_an_authenticated_caller_gets_their_own_bucket(self) -> None:
        """Otherwise one noisy client exhausts the quota of everyone behind its address."""
        token = jwt.encode({"sub": "user-1"}, _A_KEY_LONG_ENOUGH_TO_NOT_WARN, algorithm="HS256")

        assert identify(self._request(header=f"Bearer {token}")) == "user:user-1"

    def test_the_signature_is_not_checked_here(self) -> None:
        """Deliberate, and safe: the claim only picks a counter, never grants access.

        Forging one buys a different bucket. `get_current_user` still verifies the
        signature before anything is served.
        """
        forged = jwt.encode({"sub": "user-2"}, _A_DIFFERENT_KEY_LONG_ENOUGH, algorithm="HS256")

        assert identify(self._request(header=f"Bearer {forged}")) == "user:user-2"

    @pytest.mark.parametrize(
        "header",
        [None, "Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "Bearer ", "garbage"],
    )
    def test_without_a_usable_token_it_falls_back_to_the_address(self, header: str | None) -> None:
        assert identify(self._request(header=header)) == "ip:203.0.113.7"

    def test_a_request_with_no_peer_falls_back_to_a_constant(self) -> None:
        """What the in-process test transport looks like — see UNTHROTTLED in conftest."""
        assert identify(self._request(header=None, host=None)) == "ip:unknown"
