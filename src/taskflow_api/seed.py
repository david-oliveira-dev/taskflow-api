"""Demo data, so the API is usable within a minute of starting it.

A service that comes up with an empty database wastes the one moment somebody is willing to
try it. This creates the smallest set that makes every feature reachable: an administrator
(the only role that can read `GET /audit`), three ordinary accounts holding the three
project roles, a project, and a few tasks in different states.

Re-running is safe. Accounts are matched by email and reused, so `make seed` after a restart
tops the data up instead of failing on a duplicate.

The passwords here are fixed and public **on purpose** — this is demo data and the values
are printed at the end. It exists to be logged into by whoever is evaluating the project.
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.config import Settings, get_settings
from taskflow_api.db.models import Project, User
from taskflow_api.db.session import create_engine, create_session_factory
from taskflow_api.enums import GlobalRole, ProjectRole, TaskStatus
from taskflow_api.services import security
from taskflow_api.services.projects import ProjectService
from taskflow_api.services.tasks import TaskService

DEMO_PASSWORD = "demo-password-long-enough"  # noqa: S105 - demo data, printed below

ADMIN_EMAIL = "admin@example.com"
OWNER_EMAIL = "owner@example.com"
EDITOR_EMAIL = "editor@example.com"
VIEWER_EMAIL = "viewer@example.com"

PROJECT_NAME = "Apollo"


async def _account(
    session: AsyncSession, *, email: str, full_name: str, role: GlobalRole = GlobalRole.USER
) -> User:
    """Fetch the account with this email, or create it."""
    existing = await session.execute(select(User).where(User.email == email))
    found = existing.scalar_one_or_none()
    if found is not None:
        return found

    user = User(
        email=email,
        hashed_password=security.hash_password(DEMO_PASSWORD),
        full_name=full_name,
        global_role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def seed(session: AsyncSession) -> dict[str, uuid.UUID]:
    """Populate the demo data and return the ids worth quoting.

    Args:
        session: An open session. The caller owns the transaction, which is what lets the
            tests run this against a throwaway database and roll it back.

    Returns:
        A mapping of label to id, for printing or asserting on.
    """
    admin = await _account(session, email=ADMIN_EMAIL, full_name="Ada Admin", role=GlobalRole.ADMIN)
    owner = await _account(session, email=OWNER_EMAIL, full_name="Olive Owner")
    editor = await _account(session, email=EDITOR_EMAIL, full_name="Eli Editor")
    viewer = await _account(session, email=VIEWER_EMAIL, full_name="Vera Viewer")

    existing = await session.execute(
        select(Project).where(Project.name == PROJECT_NAME, Project.owner_id == owner.id)
    )
    project = existing.scalar_one_or_none()

    if project is None:
        # Through the services, not the repositories. The repositories would be shorter, but
        # they skip the audit layer — and demo data that leaves no trail undercuts the one
        # thing this project is meant to demonstrate. Seeding this way means `GET /audit`
        # has something to show the moment the service comes up, and the entries are the
        # real ones, produced by the same code path an API call takes.
        projects = ProjectService(session)
        tasks = TaskService(session)

        project = await projects.create(
            actor=owner,
            name=PROJECT_NAME,
            description="Demo project. The owner was made a member automatically.",
        )
        for member, role in ((editor, ProjectRole.EDITOR), (viewer, ProjectRole.VIEWER)):
            await projects.add_member(actor=owner, project=project, user_id=member.id, role=role)

        await tasks.create(
            actor=owner,
            project=project,
            title="Wire the telemetry",
            description="Assigned to the editor, who is a member — the rule the API enforces.",
            status=TaskStatus.DOING,
            priority=1,
            assignee_id=editor.id,
        )
        await tasks.create(
            actor=owner,
            project=project,
            title="Write the launch checklist",
            description=None,
            status=TaskStatus.TODO,
            priority=3,
            assignee_id=None,
        )
        await tasks.create(
            actor=owner,
            project=project,
            title="Confirm the abort criteria",
            description=None,
            status=TaskStatus.DONE,
            priority=2,
            assignee_id=owner.id,
        )

    return {
        "admin": admin.id,
        "owner": owner.id,
        "editor": editor.id,
        "viewer": viewer.id,
        "project": project.id,
    }


async def _run(settings: Settings) -> None:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            ids = await seed(session)
            await session.commit()
    finally:
        await engine.dispose()

    print("Seeded. Every account uses the same password:\n")  # noqa: T201
    print(f"  password: {DEMO_PASSWORD}\n")  # noqa: T201
    for label, email in (
        ("admin  (reads GET /audit)", ADMIN_EMAIL),
        ("owner  (manages membership)", OWNER_EMAIL),
        ("editor (creates and edits tasks)", EDITOR_EMAIL),
        ("viewer (read-only)", VIEWER_EMAIL),
    ):
        print(f"  {label:<34} {email}")  # noqa: T201
    print(f"\n  project id: {ids['project']}")  # noqa: T201
    print("\nNext: open docs/walkthrough.http, or POST /auth/login to get a token.")  # noqa: T201


def main() -> None:
    """Entry point for `make seed`."""
    asyncio.run(_run(get_settings()))


if __name__ == "__main__":
    main()
