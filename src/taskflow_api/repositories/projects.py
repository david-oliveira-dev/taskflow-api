"""Data access for projects and their membership."""

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from taskflow_api.db.models import Membership, Project
from taskflow_api.enums import ProjectRole
from taskflow_api.exceptions import ConflictError
from taskflow_api.pagination import CursorPosition, Page
from taskflow_api.repositories.base import after_position, paginate


def _project_position(project: Project) -> CursorPosition:
    return CursorPosition(created_at=project.created_at, entity_id=project.id)


def _membership_position(membership: Membership) -> CursorPosition:
    # Membership has no surrogate id, so `user_id` is the tie-breaker. It is unique within
    # a project, which is all the total ordering needs.
    return CursorPosition(created_at=membership.created_at, entity_id=membership.user_id)


class ProjectRepository:
    """Reads and writes `Project` and `Membership` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, name: str, owner_id: uuid.UUID, description: str | None = None
    ) -> Project:
        """Insert a project and make its owner a member with the `owner` role.

        The membership is created here rather than left to the caller: a project whose owner
        is not a member would pass every authorisation check by accident and fail every
        listing that goes through membership.
        """
        project = Project(name=name, owner_id=owner_id, description=description)
        self._session.add(project)
        await self._session.flush()

        self._session.add(
            Membership(user_id=owner_id, project_id=project.id, project_role=ProjectRole.OWNER)
        )
        await self._session.flush()
        return project

    async def get(self, project_id: uuid.UUID) -> Project | None:
        """Return a project by id, or None. Archived projects are still returned."""
        return await self._session.get(Project, project_id)

    async def add_member(
        self, *, project_id: uuid.UUID, user_id: uuid.UUID, role: ProjectRole
    ) -> Membership:
        """Grant a user a role in a project.

        Raises:
            ConflictError: The user is already a member. The composite primary key makes
                this a database guarantee rather than a check the service could forget.
        """
        membership = Membership(user_id=user_id, project_id=project_id, project_role=role)
        # Savepoint for the same reason as in UserRepository.create: a duplicate membership
        # must be recoverable without poisoning the caller's transaction.
        try:
            async with self._session.begin_nested():
                self._session.add(membership)
                await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError("User is already a member of this project.") from exc
        return membership

    async def get_membership(
        self, *, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> Membership | None:
        """Return a user's membership in a project, or None."""
        return await self._session.get(Membership, (user_id, project_id))

    async def remove_member(self, *, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Revoke a membership.

        Returns:
            True if a membership was removed, False if there was none.
        """
        # A DELETE always yields a CursorResult; the overload of `execute` is typed for
        # the general case, so the narrowing has to be explicit to reach `rowcount`.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(Membership).where(
                    Membership.project_id == project_id, Membership.user_id == user_id
                )
            ),
        )
        return result.rowcount > 0

    async def count_owners(self, project_id: uuid.UUID) -> int:
        """How many members hold the `owner` role.

        Used to enforce that the last owner cannot be removed or demoted, which would leave
        a project nobody can administer.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.project_id == project_id,
                Membership.project_role == ProjectRole.OWNER,
            )
        )
        return result.scalar_one()

    async def list_for_user(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        after: CursorPosition | None = None,
        include_archived: bool = False,
    ) -> Page[Project]:
        """List the projects a user is a member of, newest first.

        Args:
            user_id: Whose memberships to follow.
            limit: Page size, already clamped by the caller.
            after: Continue after this position.
            include_archived: Archived projects are hidden by default.

        Returns:
            A page of projects and the cursor for the next one.
        """
        stmt: Select[tuple[Project]] = (
            select(Project)
            .join(Membership, Membership.project_id == Project.id)
            .where(Membership.user_id == user_id)
            .order_by(Project.created_at.desc(), Project.id.desc())
        )
        if not include_archived:
            stmt = stmt.where(Project.is_archived.is_(False))
        if after is not None:
            stmt = stmt.where(after_position(Project.created_at, Project.id, after))

        return await paginate(self._session, stmt, limit=limit, position=_project_position)

    async def list_members(
        self, *, project_id: uuid.UUID, limit: int, after: CursorPosition | None = None
    ) -> Page[Membership]:
        """List a project's members, newest first.

        The related `User` is eagerly loaded. Under asyncio a lazy load raises instead of
        quietly issuing a query, so any caller reading `membership.user` would otherwise
        blow up at serialisation time rather than here.

        Args:
            project_id: Whose members to list.
            limit: Page size, already clamped by the caller.
            after: Continue after this position.

        Returns:
            A page of memberships with their users loaded.
        """
        stmt: Select[tuple[Membership]] = (
            select(Membership)
            .where(Membership.project_id == project_id)
            .options(selectinload(Membership.user))
            .order_by(Membership.created_at.desc(), Membership.user_id.desc())
        )
        if after is not None:
            stmt = stmt.where(after_position(Membership.created_at, Membership.user_id, after))

        return await paginate(self._session, stmt, limit=limit, position=_membership_position)
