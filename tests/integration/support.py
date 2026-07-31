"""Helpers shared by the integration tests.

Anything that reaches around the API — promoting an account to admin, planting a task
before the task endpoints exist — lives here and says why in its docstring, so that a
back door is never mistaken for a feature the API is supposed to have.
"""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from taskflow_api.db.models import AuditLog, Task, User
from taskflow_api.db.session import create_session_factory
from taskflow_api.enums import GlobalRole, TaskStatus

PASSWORD = "a-sufficiently-long-password"


def unique_email() -> str:
    """A fresh address per call — these tests commit and share one database."""
    return f"user-{uuid.uuid4()}@example.com"


def auth(token: str) -> dict[str, str]:
    """Bearer header for a token."""
    return {"Authorization": f"Bearer {token}"}


async def register(client: AsyncClient, email: str | None = None) -> dict[str, Any]:
    """Create an account through the API."""
    response = await client.post(
        "/auth/register",
        json={"email": email or unique_email(), "password": PASSWORD, "full_name": "Ada"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def login(client: AsyncClient, email: str) -> str:
    """Exchange credentials for a token."""
    response = await client.post("/auth/login", data={"username": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def new_user(client: AsyncClient) -> tuple[uuid.UUID, str]:
    """Register and log in, returning the id and a token."""
    email = unique_email()
    body = await register(client, email)
    return uuid.UUID(str(body["id"])), await login(client, email)


async def promote_to_admin(dsn: str, user_id: uuid.UUID) -> None:
    """Make an account a global admin.

    Done in the database because there is deliberately no endpoint that grants global
    roles: self-service escalation to admin would defeat the point of having the role.
    """
    await _mutate(dsn, update(User).where(User.id == user_id).values(global_role=GlobalRole.ADMIN))


async def deactivate(dsn: str, user_id: uuid.UUID) -> None:
    """Flip `is_active` off; there is no endpoint for it yet."""
    await _mutate(dsn, update(User).where(User.id == user_id).values(is_active=False))


async def plant_task(
    dsn: str, *, project_id: uuid.UUID, assignee_id: uuid.UUID, title: str = "Ship it"
) -> uuid.UUID:
    """Create a task directly.

    The task endpoints arrive in Phase 3b, but the rule "removing a member unassigns their
    tasks" belongs to Phase 3a and has to be proven now.
    """
    engine = create_async_engine(dsn)
    factory = create_session_factory(engine)
    async with factory() as session:
        task = Task(
            project_id=project_id,
            title=title,
            status=TaskStatus.TODO,
            assignee_id=assignee_id,
        )
        session.add(task)
        await session.commit()
        task_id: uuid.UUID = task.id
    await engine.dispose()
    return task_id


async def task_assignee(dsn: str, task_id: uuid.UUID) -> uuid.UUID | None:
    """Read a task's assignee straight from the database."""
    engine = create_async_engine(dsn)
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(select(Task.assignee_id).where(Task.id == task_id))
        assignee: uuid.UUID | None = result.scalar_one()
    await engine.dispose()
    return assignee


async def audit_entries(dsn: str, entity_id: uuid.UUID) -> list[AuditLog]:
    """Every audit entry about one entity, oldest first.

    Read from the table rather than through `GET /audit` so that a test can assert on the
    trail without also needing an administrator.
    """
    engine = create_async_engine(dsn)
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.at, AuditLog.id)
        )
        entries = list(result.scalars().all())
    await engine.dispose()
    return entries


async def audit_actions(dsn: str, entity_id: uuid.UUID) -> list[str]:
    """The action names recorded about one entity, oldest first."""
    return [entry.action for entry in await audit_entries(dsn, entity_id)]


async def _mutate(dsn: str, statement: Any) -> None:
    engine = create_async_engine(dsn)
    factory = create_session_factory(engine)
    async with factory() as session:
        await session.execute(statement)
        await session.commit()
    await engine.dispose()
