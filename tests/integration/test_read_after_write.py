"""Read-after-write over a real socket.

Every other test in this suite talks to the app through `httpx`'s ASGI transport, which
awaits the entire application call — including the teardown of dependencies declared with
`yield`. That makes the whole suite structurally blind to one class of bug: work done in
that teardown appears to have happened before the client sees the response, because in
process there is no "before".

A client on a socket has no such guarantee. These tests run a live uvicorn server so the
response is genuinely sent and received before the next request is made.

The bug this pins: `get_session` used to commit only in its teardown, so a 201 from
`/auth/register` reached the client before the account was durable and the immediately
following login failed with 401 — ten times out of ten. `TransactionalRoute` moves the
commit ahead of the send.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient

from tests.integration.support import PASSWORD

pytestmark = pytest.mark.integration


@pytest.fixture
async def base_url(app: FastAPI) -> AsyncIterator[str]:
    """Serve the app on a real socket, on an ephemeral port.

    Lifespan is off because the `app` fixture has already pointed `app.state` at the test
    container; letting the real lifespan run would build a second engine against the
    configured DSN instead.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())

    # ASYNC110 wants an `asyncio.Event`, but uvicorn exposes readiness only as the boolean
    # `Server.started`, so there is nothing to wait on. Polling is the supported idiom.
    while not server.started:  # noqa: ASYNC110  # pragma: no branch
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    await serving


async def test_an_account_can_log_in_immediately_after_registering(base_url: str) -> None:
    """A 201 has to mean the account exists, not that it soon will.

    Ten rounds rather than one: the failure is a timing window, and a single pass could
    close it by luck.
    """
    async with AsyncClient(base_url=base_url) as client:
        for _ in range(10):
            email = f"race-{uuid.uuid4()}@example.com"

            created = await client.post(
                "/auth/register",
                json={"email": email, "password": PASSWORD, "full_name": "Ada"},
            )
            assert created.status_code == 201, created.text

            token = await client.post("/auth/login", data={"username": email, "password": PASSWORD})

            assert token.status_code == 200, (
                "the account was acknowledged but is not visible yet — the request "
                "transaction is committing after the response is sent"
            )


async def test_a_project_can_be_read_immediately_after_creation(base_url: str) -> None:
    """The same guarantee one level up.

    Reading the project back exercises more than the row landing: the creator's `owner`
    membership is written in the same transaction, and without it the read answers 404
    rather than 200.
    """
    async with AsyncClient(base_url=base_url) as client:
        email = f"owner-{uuid.uuid4()}@example.com"
        await client.post(
            "/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Ada"},
        )
        token = (
            await client.post("/auth/login", data={"username": email, "password": PASSWORD})
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        for _ in range(5):
            created = await client.post("/projects", json={"name": "Apollo"}, headers=headers)
            assert created.status_code == 201, created.text

            readback = await client.get(f"/projects/{created.json()['id']}", headers=headers)

            assert readback.status_code == 200, "the project was created but is not readable yet"
