"""Authentication endpoints against a real database.

These go through the whole stack — router, dependency, service, repository, PostgreSQL — so
they cover what unit tests cannot: that the wiring is right and that a denied request is
denied for the reason intended.
"""

import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from taskflow_api.db.session import create_session_factory
from taskflow_api.enums import GlobalRole, ProjectRole

pytestmark = pytest.mark.integration

PASSWORD = "a-sufficiently-long-password"


def _email() -> str:
    """A unique address per call — these tests commit, so they share one database."""
    return f"user-{uuid.uuid4()}@example.com"


async def _register(client: AsyncClient, email: str | None = None) -> dict[str, object]:
    response = await client.post(
        "/auth/register",
        json={"email": email or _email(), "password": PASSWORD, "full_name": "Ada"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def _login(client: AsyncClient, email: str, password: str = PASSWORD) -> str:
    response = await client.post("/auth/login", data={"username": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


class TestRegistration:
    async def test_creates_an_account(self, client: AsyncClient) -> None:
        email = _email()

        body = await _register(client, email)

        assert body["email"] == email
        assert body["global_role"] == GlobalRole.USER
        assert body["is_active"] is True

    async def test_response_never_contains_the_password_hash(self, client: AsyncClient) -> None:
        """The reason schemas are written by hand instead of reflected from the ORM row."""
        email = _email()

        response = await client.post(
            "/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Ada"},
        )

        assert "hashed_password" not in response.text
        assert PASSWORD not in response.text

    async def test_duplicate_email_is_rejected(self, client: AsyncClient) -> None:
        email = _email()
        await _register(client, email)

        response = await client.post(
            "/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Ada"},
        )

        assert response.status_code == 409

    @pytest.mark.parametrize(
        ("field", "value"),
        [("email", "not-an-email"), ("password", "short"), ("full_name", "")],
    )
    async def test_invalid_input_is_rejected(
        self, client: AsyncClient, field: str, value: str
    ) -> None:
        payload = {"email": _email(), "password": PASSWORD, "full_name": "Ada"}
        payload[field] = value

        response = await client.post("/auth/register", json=payload)

        assert response.status_code == 422


class TestLogin:
    async def test_valid_credentials_return_a_token(self, client: AsyncClient) -> None:
        email = _email()
        await _register(client, email)

        response = await client.post("/auth/login", data={"username": email, "password": PASSWORD})

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 15 * 60
        assert body["access_token"]

    async def test_email_is_case_insensitive(self, client: AsyncClient) -> None:
        email = _email()
        await _register(client, email)

        response = await client.post(
            "/auth/login", data={"username": email.upper(), "password": PASSWORD}
        )

        assert response.status_code == 200

    async def test_wrong_password_is_rejected(self, client: AsyncClient) -> None:
        email = _email()
        await _register(client, email)

        response = await client.post(
            "/auth/login", data={"username": email, "password": "the-wrong-password"}
        )

        assert response.status_code == 401

    async def test_unknown_and_wrong_password_are_indistinguishable(
        self, client: AsyncClient
    ) -> None:
        """The user-enumeration guard, at the HTTP boundary.

        A different status, or a different message, would let anyone check whether an
        address has an account here.

        The `error` object is compared rather than the whole body: since Phase 4 every
        response also carries a per-request `correlation_id`, which differs by construction
        and says nothing about the account. Comparing the part that could leak keeps the
        test's teeth — a reworded message for one case still fails it.
        """
        email = _email()
        await _register(client, email)

        wrong_password = await client.post(
            "/auth/login", data={"username": email, "password": "the-wrong-password"}
        )
        unknown_email = await client.post(
            "/auth/login", data={"username": _email(), "password": PASSWORD}
        )

        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json()["error"] == unknown_email.json()["error"]
        assert wrong_password.headers.get("www-authenticate") == unknown_email.headers.get(
            "www-authenticate"
        )

    async def test_deactivated_account_cannot_log_in(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        email = _email()
        await _register(client, email)
        await _deactivate(app, email)

        response = await client.post("/auth/login", data={"username": email, "password": PASSWORD})

        assert response.status_code == 401


class TestCurrentUser:
    async def test_returns_the_authenticated_account(self, client: AsyncClient) -> None:
        email = _email()
        await _register(client, email)
        token = await _login(client, email)

        response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json()["email"] == email

    async def test_without_a_token_it_is_denied(self, client: AsyncClient) -> None:
        response = await client.get("/auth/me")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer not-a-token",
            "Bearer a.b.c",
            "Basic dXNlcjpwYXNz",
            "totally-malformed",
        ],
    )
    async def test_a_bad_authorization_header_is_denied(
        self, client: AsyncClient, header: str
    ) -> None:
        response = await client.get("/auth/me", headers={"Authorization": header})

        assert response.status_code == 401

    async def test_a_token_for_a_deleted_account_is_denied(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """A signature can stay valid longer than the account it names."""
        email = _email()
        body = await _register(client, email)
        token = await _login(client, email)

        await _delete_user(migrated_dsn, uuid.UUID(str(body["id"])))

        response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 401

    async def test_a_token_issued_before_deactivation_stops_working(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """Why `is_active` is checked on every request.

        Tokens are not revocable here, so one minted before the account was disabled stays
        cryptographically valid. This check is the only thing stopping it.
        """
        email = _email()
        await _register(client, email)
        token = await _login(client, email)
        assert (
            await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 200

        await _deactivate(app, email)

        response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 401


class TestProjectRoleDependency:
    """The per-project authorisation dependency, exercised through a throwaway route."""

    async def test_a_member_with_a_high_enough_role_passes(
        self, app: FastAPI, client: AsyncClient, migrated_dsn: str
    ) -> None:
        email = _email()
        body = await _register(client, email)
        token = await _login(client, email)
        project_id = await _make_project(migrated_dsn, uuid.UUID(str(body["id"])))

        response = await _call_guarded_route(app, client, project_id, token, ProjectRole.VIEWER)

        assert response == 200

    async def test_a_member_with_too_low_a_role_gets_403(
        self, app: FastAPI, client: AsyncClient, migrated_dsn: str
    ) -> None:
        email = _email()
        body = await _register(client, email)
        token = await _login(client, email)
        project_id = await _make_project(
            migrated_dsn, uuid.UUID(str(body["id"])), role=ProjectRole.VIEWER
        )

        response = await _call_guarded_route(app, client, project_id, token, ProjectRole.OWNER)

        assert response == 403

    async def test_a_non_member_gets_404_not_403(
        self, app: FastAPI, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """404 on purpose.

        Answering 403 would confirm the project exists, which turns id probing into a
        directory of everyone else's projects.
        """
        owner = await _register(client)
        outsider_email = _email()
        await _register(client, outsider_email)
        outsider_token = await _login(client, outsider_email)
        project_id = await _make_project(migrated_dsn, uuid.UUID(str(owner["id"])))

        response = await _call_guarded_route(
            app, client, project_id, outsider_token, ProjectRole.VIEWER
        )

        assert response == 404

    async def test_a_missing_project_gets_404(self, app: FastAPI, client: AsyncClient) -> None:
        email = _email()
        await _register(client, email)
        token = await _login(client, email)

        response = await _call_guarded_route(app, client, uuid.uuid4(), token, ProjectRole.VIEWER)

        assert response == 404


# --------------------------------------------------------------------------- helpers


async def _deactivate(app: FastAPI, email: str) -> None:
    """Flip `is_active` off directly; there is no endpoint for it yet."""
    from sqlalchemy import update

    from taskflow_api.db.models import User

    factory = create_session_factory(app.state.engine)
    async with factory() as session:
        await session.execute(
            update(User).where(User.email == email.lower()).values(is_active=False)
        )
        await session.commit()


async def _delete_user(dsn: str, user_id: uuid.UUID) -> None:
    from sqlalchemy import delete

    from taskflow_api.db.models import User

    engine = create_async_engine(dsn)
    factory = create_session_factory(engine)
    async with factory() as session:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()
    await engine.dispose()


async def _make_project(
    dsn: str, owner_id: uuid.UUID, role: ProjectRole = ProjectRole.OWNER
) -> uuid.UUID:
    """Create a project with the owner holding `role`, bypassing the (not yet built) API."""
    from taskflow_api.repositories.projects import ProjectRepository

    engine = create_async_engine(dsn)
    factory = create_session_factory(engine)
    async with factory() as session:
        repo = ProjectRepository(session)
        project = await repo.create(name="Apollo", owner_id=owner_id)
        if role is not ProjectRole.OWNER:
            membership = await repo.get_membership(project_id=project.id, user_id=owner_id)
            assert membership is not None
            membership.project_role = role
        await session.commit()
        project_id: uuid.UUID = project.id
    await engine.dispose()
    return project_id


async def _call_guarded_route(
    app: FastAPI,
    client: AsyncClient,
    project_id: uuid.UUID,
    token: str,
    required: ProjectRole,
) -> int:
    """Mount a route behind `require_project_role` and call it, returning the status.

    The project endpoints arrive in Phase 3a; this exercises the dependency on its own so
    the authorisation rules are proven before anything depends on them.
    """
    from fastapi import Depends

    from taskflow_api.api.deps import require_project_role

    path = f"/_probe/{required.value}/{{project_id}}"
    if not any(getattr(r, "path", None) == path for r in app.routes):

        async def _probe() -> dict[str, str]:
            return {"ok": "true"}

        app.add_api_route(
            path,
            _probe,
            dependencies=[Depends(require_project_role(required))],
        )

    response = await client.get(
        f"/_probe/{required.value}/{project_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return response.status_code
