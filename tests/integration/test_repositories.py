"""Repositories against a real PostgreSQL.

These exercise the guarantees that only a real database can prove: unique constraints,
foreign key actions, check constraints, and keyset pagination under concurrent inserts.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.db.models import AuditLog, Task, User
from taskflow_api.enums import ProjectRole, TaskStatus
from taskflow_api.exceptions import ConflictError
from taskflow_api.pagination import CursorPosition, decode_cursor
from taskflow_api.repositories.projects import ProjectRepository
from taskflow_api.repositories.tasks import TaskRepository
from taskflow_api.repositories.users import UserRepository

pytestmark = pytest.mark.integration


async def _make_user(session: AsyncSession, email: str = "ada@example.com") -> User:
    return await UserRepository(session).create(
        email=email, hashed_password="hashed", full_name="Ada Lovelace"
    )


class TestUserRepository:
    async def test_create_and_fetch_roundtrip(self, session: AsyncSession) -> None:
        user = await _make_user(session)

        assert user.id is not None
        assert await UserRepository(session).get(user.id) is user

    async def test_email_is_stored_and_matched_lowercased(self, session: AsyncSession) -> None:
        await _make_user(session, email="Ada@Example.COM")

        found = await UserRepository(session).get_by_email("ada@example.com")

        assert found is not None
        assert found.email == "ada@example.com"

    async def test_duplicate_email_raises_conflict(self, session: AsyncSession) -> None:
        await _make_user(session)

        with pytest.raises(ConflictError):
            await _make_user(session)

    async def test_case_variant_email_is_still_a_duplicate(self, session: AsyncSession) -> None:
        """Without lowercasing, signing up twice with different casing would succeed."""
        await _make_user(session, email="ada@example.com")

        with pytest.raises(ConflictError):
            await _make_user(session, email="ADA@EXAMPLE.COM")

    async def test_missing_user_is_none(self, session: AsyncSession) -> None:
        assert await UserRepository(session).get(uuid.uuid4()) is None

    async def test_session_survives_a_conflict(self, session: AsyncSession) -> None:
        """The reason the insert runs inside a SAVEPOINT.

        PostgreSQL aborts the whole transaction when a statement fails. Without a savepoint,
        catching ConflictError would leave the session poisoned and every later statement
        would fail with "current transaction is aborted" — so a service could never recover
        from a duplicate and continue in the same unit of work.
        """
        repo = UserRepository(session)
        await repo.create(email="ada@example.com", hashed_password="h", full_name="Ada")

        with pytest.raises(ConflictError):
            await repo.create(email="ada@example.com", hashed_password="h", full_name="Ada")

        # The session still works, and the first user is still there.
        recovered = await repo.create(
            email="grace@example.com", hashed_password="h", full_name="Grace"
        )
        assert recovered.id is not None
        assert await repo.get_by_email("ada@example.com") is not None


class TestProjectRepository:
    async def test_creating_a_project_makes_the_owner_a_member(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        repo = ProjectRepository(session)

        project = await repo.create(name="Apollo", owner_id=user.id)

        membership = await repo.get_membership(project_id=project.id, user_id=user.id)
        assert membership is not None
        assert membership.project_role is ProjectRole.OWNER

    async def test_adding_the_same_member_twice_raises_conflict(
        self, session: AsyncSession
    ) -> None:
        owner = await _make_user(session)
        other = await _make_user(session, email="grace@example.com")
        repo = ProjectRepository(session)
        project = await repo.create(name="Apollo", owner_id=owner.id)

        await repo.add_member(project_id=project.id, user_id=other.id, role=ProjectRole.EDITOR)

        with pytest.raises(ConflictError):
            await repo.add_member(project_id=project.id, user_id=other.id, role=ProjectRole.VIEWER)

    async def test_count_owners_tracks_the_owner_role(self, session: AsyncSession) -> None:
        owner = await _make_user(session)
        other = await _make_user(session, email="grace@example.com")
        repo = ProjectRepository(session)
        project = await repo.create(name="Apollo", owner_id=owner.id)

        assert await repo.count_owners(project.id) == 1

        await repo.add_member(project_id=project.id, user_id=other.id, role=ProjectRole.OWNER)
        assert await repo.count_owners(project.id) == 2

    async def test_remove_member_reports_whether_it_removed_anything(
        self, session: AsyncSession
    ) -> None:
        owner = await _make_user(session)
        repo = ProjectRepository(session)
        project = await repo.create(name="Apollo", owner_id=owner.id)

        assert await repo.remove_member(project_id=project.id, user_id=owner.id) is True
        assert await repo.remove_member(project_id=project.id, user_id=owner.id) is False

    async def test_archived_projects_are_hidden_by_default(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        repo = ProjectRepository(session)
        visible = await repo.create(name="Apollo", owner_id=user.id)
        archived = await repo.create(name="Gemini", owner_id=user.id)
        archived.is_archived = True
        await session.flush()

        default = await repo.list_for_user(user_id=user.id, limit=10)
        including = await repo.list_for_user(user_id=user.id, limit=10, include_archived=True)

        assert [p.id for p in default.items] == [visible.id]
        assert {p.id for p in including.items} == {visible.id, archived.id}


class TestKeysetPagination:
    async def test_pages_cover_every_row_exactly_once(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        repo = ProjectRepository(session)
        for i in range(7):
            await repo.create(name=f"Project {i}", owner_id=user.id)

        seen: list[uuid.UUID] = []
        cursor: CursorPosition | None = None
        while True:
            page = await repo.list_for_user(user_id=user.id, limit=3, after=cursor)
            seen.extend(p.id for p in page.items)
            if not page.has_more or page.next_cursor is None:
                break
            cursor = decode_cursor(page.next_cursor)

        assert len(seen) == 7
        assert len(set(seen)) == 7, "a row was returned on two different pages"

    async def test_inserting_between_pages_does_not_skip_a_row(self, session: AsyncSession) -> None:
        """The failure mode offset pagination has and keyset does not.

        With OFFSET, inserting a newer row shifts everything down by one and the first row
        of page two is silently never returned.
        """
        user = await _make_user(session)
        repo = ProjectRepository(session)
        for i in range(4):
            await repo.create(name=f"Original {i}", owner_id=user.id)

        first = await repo.list_for_user(user_id=user.id, limit=2)
        assert first.next_cursor is not None

        # A new project appears at the top of the ordering, between the two requests.
        await repo.create(name="Interloper", owner_id=user.id)

        second = await repo.list_for_user(
            user_id=user.id, limit=2, after=decode_cursor(first.next_cursor)
        )

        overlap = {p.id for p in first.items} & {p.id for p in second.items}
        assert not overlap, "the same row came back on both pages"
        assert len(second.items) == 2, "a row was skipped by the insert"

    async def test_last_page_has_no_cursor(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        repo = ProjectRepository(session)
        await repo.create(name="Only", owner_id=user.id)

        page = await repo.list_for_user(user_id=user.id, limit=10)

        assert page.has_more is False
        assert page.next_cursor is None

    async def test_empty_result_is_a_clean_empty_page(self, session: AsyncSession) -> None:
        user = await _make_user(session)

        page = await ProjectRepository(session).list_for_user(user_id=user.id, limit=10)

        assert page.items == []
        assert page.next_cursor is None
        assert page.has_more is False


class TestTaskRepository:
    async def test_status_filter_narrows_the_listing(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        project = await ProjectRepository(session).create(name="Apollo", owner_id=user.id)
        repo = TaskRepository(session)
        await repo.create(project_id=project.id, title="A", status=TaskStatus.TODO)
        done = await repo.create(project_id=project.id, title="B", status=TaskStatus.DONE)

        page = await repo.list_for_project(project_id=project.id, limit=10, status=TaskStatus.DONE)

        assert [t.id for t in page.items] == [done.id]

    async def test_priority_outside_the_allowed_range_is_rejected(
        self, session: AsyncSession
    ) -> None:
        """The CHECK constraint is the guarantee; the service should not be the only guard."""
        user = await _make_user(session)
        project = await ProjectRepository(session).create(name="Apollo", owner_id=user.id)

        # Inside a savepoint: these repositories do not catch IntegrityError, so letting it
        # abort the outer transaction would leave the fixture rolling back a dead one.
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await TaskRepository(session).create(project_id=project.id, title="Bad", priority=9)

    async def test_unassigning_leaves_the_tasks_in_place(self, session: AsyncSession) -> None:
        owner = await _make_user(session)
        member = await _make_user(session, email="grace@example.com")
        project = await ProjectRepository(session).create(name="Apollo", owner_id=owner.id)
        repo = TaskRepository(session)
        task = await repo.create(
            project_id=project.id, title="Wire the relays", assignee_id=member.id
        )

        affected = await repo.unassign_all_for_user(project_id=project.id, user_id=member.id)

        assert affected == 1
        assert task.assignee_id is None
        assert await repo.get(task.id) is not None, "the task must survive"


class TestDatabaseGuarantees:
    """Constraints that only exist because the database enforces them."""

    async def test_deleting_a_project_cascades_to_its_tasks(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        project = await ProjectRepository(session).create(name="Apollo", owner_id=user.id)
        await TaskRepository(session).create(project_id=project.id, title="Doomed")

        await session.delete(project)
        await session.flush()

        remaining = await session.execute(select(Task).where(Task.project_id == project.id))
        assert remaining.scalars().all() == []

    async def test_audit_entries_survive_the_deletion_of_their_actor(
        self, session: AsyncSession
    ) -> None:
        """The point of ON DELETE SET NULL plus a denormalised email.

        A trail that disappears with the account, or that can no longer say who acted, is
        not a trail.
        """
        user = await _make_user(session)
        entry = AuditLog(
            actor_id=user.id,
            actor_email=user.email,
            action="project.created",
            entity_type="project",
            entity_id=uuid.uuid4(),
            details={"name": "Apollo"},
        )
        session.add(entry)
        await session.flush()

        await session.delete(user)
        await session.flush()
        await session.refresh(entry)

        assert entry.actor_id is None
        assert entry.actor_email == "ada@example.com"

    async def test_enums_are_stored_as_their_values_not_their_names(
        self, session: AsyncSession
    ) -> None:
        """The database has to read the way the API reads.

        SQLAlchemy persists the enum member *name* by default, which would put `USER` and
        `TODO` in the columns while the API speaks `user` and `todo`. It round-trips
        perfectly through the ORM — which is why nothing else catches it — and quietly
        breaks every other consumer: `WHERE status = 'done'` from psql or a reporting tool
        matches no rows.
        """
        user = await _make_user(session)
        project = await ProjectRepository(session).create(name="Apollo", owner_id=user.id)
        await TaskRepository(session).create(project_id=project.id, title="Ship it")

        rows = await session.execute(
            text("""
                SELECT (SELECT global_role FROM users WHERE id = :user_id),
                       (SELECT project_role FROM memberships WHERE user_id = :user_id),
                       (SELECT status FROM tasks WHERE project_id = :project_id)
            """),
            {"user_id": user.id, "project_id": project.id},
        )

        assert rows.one() == ("user", "owner", "todo")

    async def test_an_enum_column_rejects_a_value_outside_the_set(
        self, session: AsyncSession
    ) -> None:
        """The CHECK constraint the module docstring promises actually exists.

        `native_enum=False` alone does not create it — `create_constraint` has defaulted to
        False since SQLAlchemy 1.4, which leaves a bare VARCHAR accepting any string at all.

        Two of the three rejected values are `OWNER` and `TODO`, which is exactly what the
        old name-based mapping wrote. So this also pins the storage format: if the columns
        ever go back to holding names, the constraint stops them.
        """
        user = await _make_user(session)
        project = await ProjectRepository(session).create(name="Apollo", owner_id=user.id)
        task = await TaskRepository(session).create(project_id=project.id, title="Ship it")

        rejected = [
            (text("UPDATE users SET global_role = 'SUPERUSER' WHERE id = :id"), {"id": user.id}),
            (
                text("UPDATE memberships SET project_role = 'OWNER' WHERE user_id = :id"),
                {"id": user.id},
            ),
            (text("UPDATE tasks SET status = 'TODO' WHERE id = :id"), {"id": task.id}),
        ]

        for statement, params in rejected:
            # A savepoint per attempt: in PostgreSQL a failed statement aborts the whole
            # transaction, so without one the second case could never run.
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(statement, params)

    async def test_audit_details_are_stored_in_the_metadata_column(
        self, session: AsyncSession
    ) -> None:
        """`details` on the object, `metadata` in the database.

        The attribute cannot be called `metadata` — that name is reserved on the declarative
        base — so this pins the mapping in place.
        """
        user = await _make_user(session)
        entity_id = uuid.uuid4()
        session.add(
            AuditLog(
                actor_id=user.id,
                actor_email=user.email,
                action="task.created",
                entity_type="task",
                entity_id=entity_id,
                details={"title": "Wire the relays"},
            )
        )
        await session.flush()

        # Anchored to the row this test wrote. An unqualified SELECT over `audit_log` only
        # worked while nothing else populated the table, and the API tests now do.
        row = await session.execute(
            text('SELECT "metadata" FROM audit_log WHERE entity_id = :entity_id'),
            {"entity_id": entity_id},
        )
        assert row.scalar_one() == {"title": "Wire the relays"}

    async def test_a_project_cannot_reference_a_missing_owner(self, session: AsyncSession) -> None:
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await ProjectRepository(session).create(name="Orphan", owner_id=uuid.uuid4())
