"""Idempotency, including the concurrent cases that are the whole point of it.

A sequential test passes even when the implementation is wrong: check-then-insert looks
correct until two requests overlap. So the cases that matter here fire real concurrent
requests and assert on the **effect** — how many projects exist afterwards — rather than
only on status codes.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from taskflow_api.db.models import IdempotencyKey, Project
from taskflow_api.db.session import create_session_factory
from taskflow_api.enums import IdempotencyStatus
from taskflow_api.services.idempotency import IdempotencyStore, fingerprint
from tests.integration.support import auth, new_user

pytestmark = pytest.mark.integration

HEADER = "Idempotency-Key"


def _key() -> str:
    return f"key-{uuid.uuid4()}"


async def _count_projects(dsn: str, name: str) -> int:
    engine = create_async_engine(dsn)
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            select(func.count()).select_from(Project).where(Project.name == name)
        )
        total: int = result.scalar_one()
    await engine.dispose()
    return total


class TestReplay:
    async def test_repeating_a_request_returns_the_first_response(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, token = await new_user(client)
        key = _key()
        name = f"Apollo-{uuid.uuid4()}"
        payload = {"name": name}

        first = await client.post("/projects", json=payload, headers={**auth(token), HEADER: key})
        second = await client.post("/projects", json=payload, headers={**auth(token), HEADER: key})

        assert first.status_code == second.status_code == 201
        assert first.json() == second.json(), "the replay must be the original response"
        assert second.headers["Idempotent-Replay"] == "true"
        assert await _count_projects(migrated_dsn, name) == 1, "created twice"

    async def test_the_same_key_with_a_different_body_is_refused(self, client: AsyncClient) -> None:
        """Answering with the first response would tell the caller their payload was
        applied when it never was — worse than failing.
        """
        _, token = await new_user(client)
        key = _key()
        await client.post("/projects", json={"name": "First"}, headers={**auth(token), HEADER: key})

        response = await client.post(
            "/projects", json={"name": "Second"}, headers={**auth(token), HEADER: key}
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "idempotency_key_reused"
        # The envelope promises `message` is prose. Raising with the bare key put the key
        # itself there, saying nothing about what went wrong — found by hand, pinned here.
        assert key not in body["error"]["message"]
        assert "different request body" in body["error"]["message"]

    async def test_keys_are_scoped_per_account(self, client: AsyncClient) -> None:
        """One client must not be able to collide with — or probe — another's keys."""
        _, first_token = await new_user(client)
        _, second_token = await new_user(client)
        key = _key()

        await client.post(
            "/projects", json={"name": "Mine"}, headers={**auth(first_token), HEADER: key}
        )
        response = await client.post(
            "/projects", json={"name": "Theirs"}, headers={**auth(second_token), HEADER: key}
        )

        assert response.status_code == 201

    async def test_a_request_without_the_header_is_unaffected(self, client: AsyncClient) -> None:
        """The header is optional; requiring it would break every existing client."""
        _, token = await new_user(client)

        first = await client.post("/projects", json={"name": "A"}, headers=auth(token))
        second = await client.post("/projects", json={"name": "A"}, headers=auth(token))

        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]

    @pytest.mark.parametrize("key", ["", "   ", "x" * 256])
    async def test_a_malformed_key_is_rejected(self, client: AsyncClient, key: str) -> None:
        _, token = await new_user(client)

        response = await client.post(
            "/projects", json={"name": "A"}, headers={**auth(token), HEADER: key}
        )

        assert response.status_code == 422

    async def test_it_applies_to_task_creation_too(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project = (await client.post("/projects", json={"name": "P"}, headers=auth(token))).json()
        key = _key()
        payload = {"title": "Ship it"}

        first = await client.post(
            f"/projects/{project['id']}/tasks",
            json=payload,
            headers={**auth(token), HEADER: key},
        )
        second = await client.post(
            f"/projects/{project['id']}/tasks",
            json=payload,
            headers={**auth(token), HEADER: key},
        )

        assert first.json()["id"] == second.json()["id"]
        listing = await client.get(f"/projects/{project['id']}/tasks", headers=auth(token))
        assert len(listing.json()["items"]) == 1


class TestInFlight:
    async def test_a_key_still_being_processed_is_a_conflict(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """409, not a wait and not a reprocess.

        The reservation is planted directly so the in-flight window is deterministic; the
        genuinely concurrent case is covered below.
        """
        user_id, token = await new_user(client)
        key = _key()
        payload = {"name": "Apollo"}

        engine = create_async_engine(migrated_dsn)
        store = IdempotencyStore(create_session_factory(engine))
        await store.reserve(
            user_id=user_id,
            key=key,
            request_hash=fingerprint(b'{"name":"Apollo"}'),
        )

        response = await client.post(
            "/projects", json=payload, headers={**auth(token), HEADER: key}
        )
        await engine.dispose()

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "idempotency_key_in_progress"


class TestConcurrency:
    async def test_simultaneous_requests_create_the_resource_once(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """The case the header exists for, and the one a sequential test cannot reach.

        Five requests with the same key, fired together: exactly one may do the work, and
        the project must exist exactly once afterwards. The assertion on the effect is the
        important one — status codes alone would not catch a duplicate insert.
        """
        _, token = await new_user(client)
        key = _key()
        name = f"Concurrent-{uuid.uuid4()}"
        headers = {**auth(token), HEADER: key}

        responses = await asyncio.gather(
            *(client.post("/projects", json={"name": name}, headers=headers) for _ in range(5))
        )

        codes = sorted(r.status_code for r in responses)
        assert codes.count(201) == 1, f"exactly one request should do the work, got {codes}"
        assert all(code in {201, 409} for code in codes), codes
        assert await _count_projects(migrated_dsn, name) == 1, "the effect happened twice"

    async def test_concurrent_requests_with_different_bodies_do_not_both_apply(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """One key, two payloads, sent together: at most one may take effect."""
        _, token = await new_user(client)
        key = _key()
        first_name = f"First-{uuid.uuid4()}"
        second_name = f"Second-{uuid.uuid4()}"
        headers = {**auth(token), HEADER: key}

        await asyncio.gather(
            client.post("/projects", json={"name": first_name}, headers=headers),
            client.post("/projects", json={"name": second_name}, headers=headers),
        )

        created = await _count_projects(migrated_dsn, first_name) + await _count_projects(
            migrated_dsn, second_name
        )
        assert created == 1, "both payloads were applied under one key"


class TestReleaseAndExpiry:
    async def test_a_failed_request_frees_its_key(self, client: AsyncClient) -> None:
        """Otherwise the retry that would have worked is answered 409 for a day."""
        _, token = await new_user(client)
        key = _key()

        rejected = await client.post(
            "/projects",
            json={"name": ""},  # fails validation
            headers={**auth(token), HEADER: key},
        )
        assert rejected.status_code == 422

        accepted = await client.post(
            "/projects", json={"name": "Recovered"}, headers={**auth(token), HEADER: key}
        )

        assert accepted.status_code == 201

    async def test_an_expired_key_can_be_reused(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """After the retention window the client was told to stop relying on the record."""
        user_id, token = await new_user(client)
        key = _key()
        engine = create_async_engine(migrated_dsn)
        store = IdempotencyStore(create_session_factory(engine))

        long_ago = datetime.now(UTC) - timedelta(days=2)
        await store.reserve(user_id=user_id, key=key, request_hash=fingerprint(b"{}"), now=long_ago)

        response = await client.post(
            "/projects", json={"name": "Reused"}, headers={**auth(token), HEADER: key}
        )
        await engine.dispose()

        assert response.status_code == 201

    async def test_purging_removes_only_expired_records(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """Without a sweep the table only grows."""
        user_id, _ = await new_user(client)
        engine = create_async_engine(migrated_dsn)
        factory = create_session_factory(engine)
        store = IdempotencyStore(factory)

        stale = _key()
        fresh = _key()
        await store.reserve(
            user_id=user_id,
            key=stale,
            request_hash=fingerprint(b"{}"),
            now=datetime.now(UTC) - timedelta(days=2),
        )
        await store.reserve(user_id=user_id, key=fresh, request_hash=fingerprint(b"{}"))

        removed = await store.purge_expired()

        async with factory() as session:
            assert await session.get(IdempotencyKey, (user_id, stale)) is None
            assert await session.get(IdempotencyKey, (user_id, fresh)) is not None
        await engine.dispose()
        assert removed >= 1

    async def test_a_completed_record_keeps_the_response(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """What makes the replay possible at all, checked in the table rather than inferred."""
        user_id, token = await new_user(client)
        key = _key()

        created = await client.post(
            "/projects", json={"name": "Stored"}, headers={**auth(token), HEADER: key}
        )

        engine = create_async_engine(migrated_dsn)
        factory = create_session_factory(engine)
        async with factory() as session:
            record = await session.get(IdempotencyKey, (user_id, key))
            assert record is not None
            assert record.status == IdempotencyStatus.COMPLETED
            assert record.status_code == 201
            assert record.response_snapshot == created.json()
        await engine.dispose()
