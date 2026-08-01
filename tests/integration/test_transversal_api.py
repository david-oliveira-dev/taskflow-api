"""The cross-cutting concerns, end to end: error envelope, readiness, rate limit.

Idempotency has its own file, because its interesting cases are concurrent.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from taskflow_api.api.correlation import HEADER_NAME
from taskflow_api.config import Settings
from tests.integration.conftest import build_app
from tests.integration.support import auth, new_user, unique_email

pytestmark = pytest.mark.integration


class TestErrorEnvelope:
    async def test_every_failure_uses_the_same_shape(self, client: AsyncClient) -> None:
        response = await client.get(f"/projects/{uuid.uuid4()}")

        body = response.json()
        assert set(body) == {"error", "correlation_id"}
        assert set(body["error"]) == {"code", "message", "detail"}

    async def test_the_code_is_stable_and_separate_from_the_message(
        self, client: AsyncClient
    ) -> None:
        """Clients branch on `code`; `message` is prose and may be reworded."""
        _, token = await new_user(client)

        response = await client.get(f"/projects/{uuid.uuid4()}", headers=auth(token))

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_a_validation_failure_reports_the_offending_fields(
        self, client: AsyncClient
    ) -> None:
        _, token = await new_user(client)

        response = await client.post("/projects", json={"name": ""}, headers=auth(token))

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "validation_error"
        assert any("name" in str(item.get("loc", "")) for item in body["error"]["detail"])

    async def test_a_validation_failure_does_not_echo_the_submitted_value(
        self, client: AsyncClient
    ) -> None:
        """Pydantic reports `input` by default, which would reflect a rejected password
        straight back into the response body and from there into any log that captures it.
        """
        secret = "hunter2-hunter2-hunter2"

        response = await client.post(
            "/auth/register",
            json={"email": "not-an-email", "password": secret, "full_name": "Ada"},
        )

        assert response.status_code == 422
        assert secret not in response.text
        assert all("input" not in item for item in response.json()["error"]["detail"])

    async def test_a_domain_conflict_keeps_its_own_code(self, client: AsyncClient) -> None:
        email = unique_email()
        await client.post(
            "/auth/register",
            json={"email": email, "password": "a-sufficiently-long-password", "full_name": "A"},
        )

        response = await client.post(
            "/auth/register",
            json={"email": email, "password": "a-sufficiently-long-password", "full_name": "A"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"


class TestCorrelationId:
    async def test_every_response_carries_one(self, client: AsyncClient) -> None:
        response = await client.get("/health")

        assert response.headers[HEADER_NAME]

    async def test_the_body_and_the_header_agree_on_a_failure(self, client: AsyncClient) -> None:
        """The point of the id: a user quotes one token and it finds the request."""
        response = await client.get("/audit")

        assert response.json()["correlation_id"] == response.headers[HEADER_NAME]

    async def test_a_caller_supplied_id_is_honoured(self, client: AsyncClient) -> None:
        response = await client.get("/health", headers={HEADER_NAME: "trace-abc-123"})

        assert response.headers[HEADER_NAME] == "trace-abc-123"

    async def test_an_unsafe_supplied_id_is_replaced(self, client: AsyncClient) -> None:
        """A newline would let a caller forge what looks like a second log entry."""
        response = await client.get("/health", headers={HEADER_NAME: "a b;c"})

        assert response.headers[HEADER_NAME] != "a b;c"
        assert uuid.UUID(response.headers[HEADER_NAME])

    async def test_two_requests_get_different_ids(self, client: AsyncClient) -> None:
        first = await client.get("/health")
        second = await client.get("/health")

        assert first.headers[HEADER_NAME] != second.headers[HEADER_NAME]


class TestReadiness:
    async def test_it_reports_both_dependencies(self, client: AsyncClient) -> None:
        response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "ok", "redis": "ok"}

    async def test_a_broken_redis_is_degraded_but_still_serving(
        self, app_settings: Settings
    ) -> None:
        """Redis only carries rate-limit counters and the limiter fails open.

        Answering 503 here would take a working service out of rotation to punish the loss
        of a throttle — so the body says "degraded" and the status stays 200.
        """
        broken = app_settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
        built, engine, redis = build_app(broken)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=built), base_url="http://test"
            ) as unhealthy:
                response = await unhealthy.get("/ready")
        finally:
            await engine.dispose()
            await redis.aclose()

        assert response.status_code == 200
        assert response.json() == {
            "status": "degraded",
            "database": "ok",
            "redis": "unavailable",
        }

    async def test_a_broken_database_is_not_ready(self, app_settings: Settings) -> None:
        """Without Postgres nothing can be served, so the instance leaves rotation."""
        broken = app_settings.model_copy(
            update={"database_url": "postgresql+asyncpg://u:p@127.0.0.1:1/none"}
        )
        built, engine, redis = build_app(broken)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=built), base_url="http://test"
            ) as unhealthy:
                response = await unhealthy.get("/ready")
        finally:
            await engine.dispose()
            await redis.aclose()

        assert response.status_code == 503
        assert response.json()["database"] == "unavailable"

    async def test_liveness_never_touches_a_dependency(self, app_settings: Settings) -> None:
        """The distinction ADR 0002 exists for: a database blip must not kill the process."""
        broken = app_settings.model_copy(
            update={"database_url": "postgresql+asyncpg://u:p@127.0.0.1:1/none"}
        )
        built, engine, redis = build_app(broken)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=built), base_url="http://test"
            ) as unhealthy:
                response = await unhealthy.get("/health")
        finally:
            await engine.dispose()
            await redis.aclose()

        assert response.status_code == 200


@pytest.fixture
async def throttled(app_settings: Settings) -> AsyncIterator[FastAPI]:
    """An app with a limit small enough to reach deliberately."""
    settings = app_settings.model_copy(
        update={"rate_limit_requests": 3, "rate_limit_window_seconds": 60}
    )
    built, engine, redis = build_app(settings)

    yield built

    await engine.dispose()
    await redis.aclose()


@pytest.fixture
async def throttled_client(throttled: FastAPI, client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """A client whose every request lands in a bucket unique to this test.

    The account is created through the *unthrottled* app and the token is then used against
    the throttled one — they share a database and a signing key, so it is the same identity
    either way, and registering does not spend the small quota under test.

    Authenticating is what makes these tests deterministic. Unauthenticated in-process
    requests have no peer address, so they all count against one `ip:unknown` bucket shared
    with the rest of the suite; a `flushdb` between tests papers over that, but leaves the
    result depending on what ran before. Keying on the account removes the coupling instead
    of hiding it — and it is also how the limiter is meant to be used.
    """
    _, token = await new_user(client)
    async with AsyncClient(
        transport=ASGITransport(app=throttled),
        base_url="http://test",
        headers=auth(token),
    ) as ac:
        yield ac


class TestRateLimit:
    async def test_the_quota_is_enforced(self, throttled_client: AsyncClient) -> None:
        codes = [(await throttled_client.get("/auth/me")).status_code for _ in range(4)]

        assert codes == [200, 200, 200, 429]

    async def test_a_rejection_says_when_to_come_back(self, throttled_client: AsyncClient) -> None:
        for _ in range(3):
            await throttled_client.get("/auth/me")

        response = await throttled_client.get("/auth/me")

        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1
        assert response.headers["X-RateLimit-Limit"] == "3"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert int(response.headers["X-RateLimit-Reset"]) >= 1
        assert response.json()["error"]["code"] == "rate_limited"

    async def test_headers_are_present_on_success_too(self, throttled_client: AsyncClient) -> None:
        """A client can only back off before hitting the wall if told how close it is."""
        response = await throttled_client.get("/auth/me")

        assert response.headers["X-RateLimit-Limit"] == "3"
        assert response.headers["X-RateLimit-Remaining"] == "2"

    async def test_the_probes_are_exempt(self, throttled_client: AsyncClient) -> None:
        """A liveness probe throttled by the traffic it exists to survive gets the
        container killed in the middle of an incident.
        """
        for _ in range(10):
            assert (await throttled_client.get("/health")).status_code == 200
        assert (await throttled_client.get("/ready")).status_code == 200

    async def test_a_rejection_still_carries_a_correlation_id(
        self, throttled_client: AsyncClient
    ) -> None:
        """The limiter runs before routing, so its response has to be built by the same
        envelope as everything else — otherwise a 429 is the one failure a user cannot
        report.
        """
        for _ in range(3):
            await throttled_client.get("/auth/me")

        response = await throttled_client.get("/auth/me")

        assert response.json()["correlation_id"] == response.headers[HEADER_NAME]

    async def test_concurrent_requests_against_a_limit_of_n_minus_one(
        self, throttled_client: AsyncClient
    ) -> None:
        """Four requests fired together against a limit of three: exactly one is refused.

        This is the case that separates an atomic limiter from a racy one. `GET`, decide in
        Python, `SET` would let several of these read the same count, all conclude they are
        under the limit, and all pass — a limiter reporting a guarantee it does not provide.
        Deciding inside one Lua script is what keeps the arithmetic exact under concurrency.
        """
        responses = await asyncio.gather(*(throttled_client.get("/auth/me") for _ in range(4)))

        codes = [r.status_code for r in responses]
        assert codes.count(429) == 1, f"exactly one should be refused, got {codes}"
        assert codes.count(200) == 3, codes

    async def test_an_unreachable_redis_lets_traffic_through(self, app_settings: Settings) -> None:
        """Fail-open, proven at the HTTP boundary rather than only in the unit test."""
        settings = app_settings.model_copy(
            update={"rate_limit_requests": 1, "redis_url": "redis://127.0.0.1:1/0"}
        )
        built, engine, redis = build_app(settings)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=built), base_url="http://test"
            ) as degraded:
                codes = [(await degraded.get("/openapi.json")).status_code for _ in range(5)]
                marked = await degraded.get("/openapi.json")
        finally:
            await engine.dispose()
            await redis.aclose()

        assert codes == [200] * 5
        assert marked.headers["X-RateLimit-Degraded"] == "true"
