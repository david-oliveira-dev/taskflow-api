"""Projects and membership.

This is the orchestration layer: it sequences repository calls, applies the pure rules from
`services.rules`, and appends the audit entry — all inside the caller's transaction, which
the request-scoped session commits once. Nothing here knows about HTTP.

The ordering inside each mutation is not arbitrary. Rules are checked before writes, and the
audit entry is appended last, from state that has already been flushed. Recording first and
validating after would leave entries describing changes that never happened.
"""

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.db.models import Membership, Project, User
from taskflow_api.enums import AuditAction, AuditEntity, ProjectRole
from taskflow_api.exceptions import BusinessRuleError, NotFoundError
from taskflow_api.pagination import CursorPosition, Page
from taskflow_api.repositories.projects import ProjectRepository
from taskflow_api.repositories.tasks import TaskRepository
from taskflow_api.repositories.users import UserRepository
from taskflow_api.services import rules
from taskflow_api.services.audit import AuditService


class ProjectService:
    """Creates projects, manages their membership, and records both."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._users = UserRepository(session)
        self._tasks = TaskRepository(session)
        self._audit = AuditService(session)

    # ----------------------------------------------------------------- projects

    async def create(self, *, actor: User, name: str, description: str | None) -> Project:
        """Create a project owned by the actor.

        The repository also makes the creator an `owner` member; authorisation in this
        service goes through membership only, so a project without that row would be
        invisible to its own creator.
        """
        project = await self._projects.create(name=name, owner_id=actor.id, description=description)
        await self._audit.record(
            actor=actor,
            action=AuditAction.PROJECT_CREATED,
            entity_type=AuditEntity.PROJECT,
            entity_id=project.id,
            details={"name": name},
        )
        return project

    async def list_for_user(
        self,
        *,
        user: User,
        limit: int,
        after: CursorPosition | None,
        include_archived: bool,
    ) -> Page[Project]:
        """List the projects the user belongs to.

        Membership-scoped even for a global admin. An admin can open any project by id, but
        listing every project in the installation is a different capability from
        administering one, and conflating them turns a support action into a data dump.
        """
        return await self._projects.list_for_user(
            user_id=user.id, limit=limit, after=after, include_archived=include_archived
        )

    async def update(
        self, *, actor: User, project: Project, changes: Mapping[str, object]
    ) -> Project:
        """Apply a partial update.

        Args:
            actor: Who is making the change.
            project: The project to modify.
            changes: Only the fields the client actually sent, so that omitting a field and
                sending it as null stay distinguishable.

        Returns:
            The updated project.

        Raises:
            ConflictError: The project is archived.
        """
        rules.ensure_mutable(project)

        requested = {
            field: value
            for field, value in changes.items()
            if field in rules.UPDATABLE_PROJECT_FIELDS
        }
        current = {field: getattr(project, field) for field in rules.UPDATABLE_PROJECT_FIELDS}
        diff = rules.changed_fields(current=current, requested=requested)

        if not diff:
            # A patch that changes nothing is not an error, but it must not leave an audit
            # entry claiming an update happened.
            return project

        for field, change in diff.items():
            setattr(project, field, change["to"])
        await self._session.flush()

        await self._audit.record(
            actor=actor,
            action=AuditAction.PROJECT_UPDATED,
            entity_type=AuditEntity.PROJECT,
            entity_id=project.id,
            details={"changed": diff},
        )
        return project

    async def archive(self, *, actor: User, project: Project) -> Project:
        """Soft-delete a project.

        Archiving an already archived project is a 409 like any other write to it. Keeping
        one rule — archived projects reject every mutation — is worth more than the
        idempotent DELETE the exception would buy.

        Raises:
            ConflictError: The project is already archived.
        """
        rules.ensure_mutable(project)

        project.is_archived = True
        await self._session.flush()

        await self._audit.record(
            actor=actor,
            action=AuditAction.PROJECT_ARCHIVED,
            entity_type=AuditEntity.PROJECT,
            entity_id=project.id,
        )
        return project

    # --------------------------------------------------------------- membership

    async def list_members(
        self, *, project: Project, limit: int, after: CursorPosition | None
    ) -> Page[Membership]:
        """List a project's members, with their accounts loaded."""
        return await self._projects.list_members(project_id=project.id, limit=limit, after=after)

    async def add_member(
        self, *, actor: User, project: Project, user_id: uuid.UUID, role: ProjectRole
    ) -> tuple[Membership, User]:
        """Grant a user a role in the project.

        Returns:
            The new membership and the user it belongs to. The user is returned rather than
            read back through the relationship because it is already loaded here, and a
            lazy load would raise under asyncio.

        Raises:
            ConflictError: The project is archived, or the user is already a member.
            BusinessRuleError: There is no such user, or the account is deactivated.
        """
        rules.ensure_mutable(project)
        member = rules.ensure_can_be_member(await self._users.get(user_id))

        membership = await self._projects.add_member(
            project_id=project.id, user_id=member.id, role=role
        )
        await self._audit.record(
            actor=actor,
            action=AuditAction.MEMBER_ADDED,
            entity_type=AuditEntity.MEMBERSHIP,
            entity_id=project.id,
            details={"user_id": str(member.id), "email": member.email, "role": role.value},
        )
        return membership, member

    async def change_member_role(
        self, *, actor: User, project: Project, user_id: uuid.UUID, role: ProjectRole
    ) -> tuple[Membership, User]:
        """Change a member's role.

        Raises:
            ConflictError: The project is archived, or this would demote the only owner.
            NotFoundError: That user is not a member.
        """
        rules.ensure_mutable(project)
        membership, member = await self._require_membership(project=project, user_id=user_id)

        previous = membership.project_role
        if previous is role:
            return membership, member

        if role is not ProjectRole.OWNER:
            rules.ensure_not_last_owner(
                current_role=previous,
                owner_count=await self._projects.count_owners(project.id),
            )

        membership.project_role = role
        await self._session.flush()

        await self._audit.record(
            actor=actor,
            action=AuditAction.MEMBER_ROLE_CHANGED,
            entity_type=AuditEntity.MEMBERSHIP,
            entity_id=project.id,
            details={
                "user_id": str(member.id),
                "email": member.email,
                "from": previous.value,
                "to": role.value,
            },
        )
        return membership, member

    async def remove_member(self, *, actor: User, project: Project, user_id: uuid.UUID) -> None:
        """Revoke a membership, unassigning that member's tasks in the project.

        Removal must not destroy work, so the member's tasks stay and lose their assignee.
        How many were affected goes into the audit entry: a task silently becoming
        unassigned is otherwise indistinguishable from someone clearing it by hand.

        Raises:
            ConflictError: The project is archived, or this is the only owner.
            NotFoundError: That user is not a member.
        """
        rules.ensure_mutable(project)
        membership, member = await self._require_membership(project=project, user_id=user_id)

        rules.ensure_not_last_owner(
            current_role=membership.project_role,
            owner_count=await self._projects.count_owners(project.id),
        )

        unassigned = await self._tasks.unassign_all_for_user(project_id=project.id, user_id=user_id)
        await self._projects.remove_member(project_id=project.id, user_id=user_id)

        await self._audit.record(
            actor=actor,
            action=AuditAction.MEMBER_REMOVED,
            entity_type=AuditEntity.MEMBERSHIP,
            entity_id=project.id,
            details={
                "user_id": str(member.id),
                "email": member.email,
                "role": membership.project_role.value,
                "unassigned_tasks": unassigned,
            },
        )

    async def _require_membership(
        self, *, project: Project, user_id: uuid.UUID
    ) -> tuple[Membership, User]:
        """Load a membership and its user, or fail."""
        membership = await self._projects.get_membership(project_id=project.id, user_id=user_id)
        if membership is None:
            raise NotFoundError("That user is not a member of this project.")

        member = await self._users.get(user_id)
        if member is None:  # pragma: no cover - the foreign key makes this unreachable
            raise BusinessRuleError("No such user.")
        return membership, member
