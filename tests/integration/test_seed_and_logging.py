"""The demo seed and the access log, against a real database.

The seed is tested because it is the first thing an evaluator runs. A `make seed` that fails
on a second run, or that produces data the API then rejects, is worse than no seed at all.
"""

import json

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.config import Settings
from taskflow_api.db.models import AuditLog, Membership, Task, User
from taskflow_api.enums import GlobalRole, ProjectRole
from taskflow_api.observability import configure_logging
from taskflow_api.seed import (
    ADMIN_EMAIL,
    DEMO_PASSWORD,
    EDITOR_EMAIL,
    OWNER_EMAIL,
    VIEWER_EMAIL,
    seed,
)
from tests.integration.support import auth

pytestmark = pytest.mark.integration


def _json_logging() -> Settings:
    """Settings that force JSON output, so the assertions can parse the lines."""
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  # see [tool.pyright] in pyproject.toml
        environment="ci",
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
        jwt_secret="a-test-signing-key-long-enough-for-hs256",
    )


class TestSeed:
    async def test_it_creates_the_four_roles(self, session: AsyncSession) -> None:
        ids = await seed(session)

        assert set(ids) == {"admin", "owner", "editor", "viewer", "project"}
        admin = await session.get(User, ids["admin"])
        assert admin is not None
        assert admin.global_role is GlobalRole.ADMIN

    async def test_the_project_has_all_three_project_roles(self, session: AsyncSession) -> None:
        """So every authorisation path is reachable without creating anything first."""
        ids = await seed(session)

        result = await session.execute(
            select(Membership).where(Membership.project_id == ids["project"])
        )
        roles = {m.project_role for m in result.scalars().all()}

        assert roles == {ProjectRole.OWNER, ProjectRole.EDITOR, ProjectRole.VIEWER}

    async def test_it_leaves_an_audit_trail(self, session: AsyncSession) -> None:
        """Seeding goes through the services, not the repositories.

        Repositories would be shorter and would skip the audit layer — demo data with no
        trail undercuts the one thing this project exists to demonstrate, and `GET /audit`
        would greet an evaluator with an empty list.
        """
        ids = await seed(session)

        result = await session.execute(
            select(AuditLog.action).where(AuditLog.entity_id == ids["project"])
        )
        actions = set(result.scalars().all())

        assert "project.created" in actions
        assert "member.added" in actions

    async def test_the_tasks_respect_the_assignee_rule(self, session: AsyncSession) -> None:
        """Seed data that the API itself would reject is a trap for whoever reads it."""
        ids = await seed(session)

        tasks = (
            (await session.execute(select(Task).where(Task.project_id == ids["project"])))
            .scalars()
            .all()
        )
        members = {
            m.user_id
            for m in (
                await session.execute(
                    select(Membership).where(Membership.project_id == ids["project"])
                )
            )
            .scalars()
            .all()
        }

        assert tasks, "the seed should create tasks"
        for task in tasks:
            assert task.assignee_id is None or task.assignee_id in members

    async def test_running_it_twice_changes_nothing(self, session: AsyncSession) -> None:
        """`make seed` after a restart must top up, not fail on a duplicate email."""
        first = await seed(session)

        second = await seed(session)

        assert first == second
        # Scoped to the seeded project. An unqualified COUNT over `memberships` passes in
        # isolation and fails in a full run, because the other integration tests commit
        # their own rows to the same database.
        members = await session.execute(
            select(func.count())
            .select_from(Membership)
            .where(Membership.project_id == first["project"])
        )
        assert members.scalar_one() == 3, "membership was duplicated"

    async def test_the_seeded_accounts_can_log_in(self, app: FastAPI, client: AsyncClient) -> None:
        """The credentials printed by `make seed` have to actually work."""
        # Committed through the app's own factory, so the accounts are visible over HTTP —
        # the `session` fixture rolls back, which is right for the tests above and wrong
        # here.
        async with app.state.session_factory() as db:
            await seed(db)
            await db.commit()

        for email in (ADMIN_EMAIL, OWNER_EMAIL, EDITOR_EMAIL, VIEWER_EMAIL):
            response = await client.post(
                "/auth/login", data={"username": email, "password": DEMO_PASSWORD}
            )
            assert response.status_code == 200, f"{email} could not log in"

        admin_token = (
            await client.post(
                "/auth/login", data={"username": ADMIN_EMAIL, "password": DEMO_PASSWORD}
            )
        ).json()["access_token"]
        trail = await client.get("/audit", headers=auth(admin_token))
        assert trail.status_code == 200
        assert trail.json()["items"], "the admin should find a trail waiting"


class TestAccessLog:
    async def test_one_line_per_request_with_the_correlation_id(
        self, client: AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(_json_logging())
        capsys.readouterr()

        response = await client.get("/health")

        lines = [
            json.loads(line)
            for line in capsys.readouterr().out.strip().splitlines()
            if line.startswith("{")
        ]
        access = [entry for entry in lines if entry.get("event") == "request"]
        assert len(access) == 1
        assert access[0]["method"] == "GET"
        assert access[0]["path"] == "/health"
        assert access[0]["status"] == 200
        assert access[0]["correlation_id"] == response.headers["x-correlation-id"]
        assert isinstance(access[0]["duration_ms"], (int, float))

    async def test_the_log_line_carries_no_credentials(
        self, client: AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Not the query string, not headers, not the body.

        The first carries tokens in the wild, the second carries `Authorization`, and the
        third is where passwords are. A log stream is a place secrets get retained and
        forwarded to people who should not see them.
        """
        configure_logging(_json_logging())
        capsys.readouterr()
        password = "a-very-secret-password-value"

        await client.post(
            "/auth/register",
            json={"email": "log@example.com", "password": password, "full_name": "L"},
            headers={"Authorization": "Bearer a-token-that-must-not-be-logged"},
        )

        out = capsys.readouterr().out
        assert password not in out
        assert "a-token-that-must-not-be-logged" not in out
        assert "Authorization" not in out
