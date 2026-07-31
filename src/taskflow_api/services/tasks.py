"""Tasks.

Same shape as `services/projects.py`: sequence the repositories, apply the pure rules from
`services.rules`, append the audit entry, all inside the caller's transaction. Nothing here
knows about HTTP.

Every mutation starts by refusing to write to an archived project. A task belongs to its
project, so "the project is archived" has to stop work on its contents too — otherwise
archiving would only hide the container while its tasks stayed editable.
"""

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.db.models import Project, Task, User
from taskflow_api.enums import AuditAction, AuditEntity, TaskStatus
from taskflow_api.pagination import CursorPosition, Page
from taskflow_api.repositories.projects import ProjectRepository
from taskflow_api.repositories.tasks import TaskRepository
from taskflow_api.services import rules
from taskflow_api.services.audit import AuditService


class TaskService:
    """Creates, edits and deletes tasks, and records each change."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tasks = TaskRepository(session)
        self._projects = ProjectRepository(session)
        self._audit = AuditService(session)

    async def create(
        self,
        *,
        actor: User,
        project: Project,
        title: str,
        description: str | None,
        status: TaskStatus,
        priority: int,
        assignee_id: uuid.UUID | None,
    ) -> Task:
        """Add a task to a project.

        Raises:
            ConflictError: The project is archived.
            BusinessRuleError: The nominated assignee is not a member of the project.
        """
        rules.ensure_mutable(project)
        await self._ensure_assignable(project=project, assignee_id=assignee_id)

        task = await self._tasks.create(
            project_id=project.id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee_id=assignee_id,
        )
        await self._audit.record(
            actor=actor,
            action=AuditAction.TASK_CREATED,
            entity_type=AuditEntity.TASK,
            entity_id=task.id,
            details={
                "project_id": str(project.id),
                "title": title,
                "status": status.value,
                "assignee_id": str(assignee_id) if assignee_id else None,
            },
        )
        return task

    async def list_for_project(
        self,
        *,
        project: Project,
        limit: int,
        after: CursorPosition | None,
        status: TaskStatus | None,
    ) -> Page[Task]:
        """List a project's tasks, newest first, optionally filtered by status."""
        return await self._tasks.list_for_project(
            project_id=project.id, limit=limit, after=after, status=status
        )

    async def update(
        self, *, actor: User, project: Project, task: Task, changes: Mapping[str, object]
    ) -> Task:
        """Apply a partial update to a task.

        Args:
            actor: Who is making the change.
            project: The task's project, already loaded by the authorisation dependency.
            task: The task to modify.
            changes: Only the fields the client sent, so that omitting `assignee_id` and
                sending it as null stay distinguishable — the second means "unassign".

        Returns:
            The updated task.

        Raises:
            ConflictError: The project is archived.
            BusinessRuleError: The new assignee is not a member of the project.
        """
        rules.ensure_mutable(project)

        requested = {
            field: value for field, value in changes.items() if field in rules.UPDATABLE_TASK_FIELDS
        }
        current = {field: getattr(task, field) for field in rules.UPDATABLE_TASK_FIELDS}
        diff = rules.changed_fields(current=current, requested=requested)

        if not diff:
            return task

        if "assignee_id" in diff:
            new_assignee = diff["assignee_id"]["to"]
            assert new_assignee is None or isinstance(new_assignee, uuid.UUID)
            await self._ensure_assignable(project=project, assignee_id=new_assignee)

        for field, change in diff.items():
            setattr(task, field, change["to"])
        await self._session.flush()

        await self._audit.record(
            actor=actor,
            action=AuditAction.TASK_UPDATED,
            entity_type=AuditEntity.TASK,
            entity_id=task.id,
            # The diff carries real values because the assignment above uses them; the audit
            # copy stringifies UUIDs so it survives `json.dumps` on the way into JSONB.
            details={"project_id": str(project.id), "changed": rules.json_safe(diff)},
        )
        return task

    async def delete(self, *, actor: User, project: Project, task: Task) -> None:
        """Delete a task for good.

        The audit entry is written from the task's own fields **before** the row goes, and
        keeps its title. An entry saying only that some id was deleted is not a record of
        anything a person could act on later.

        Raises:
            ConflictError: The project is archived.
        """
        rules.ensure_mutable(project)

        details: dict[str, object] = {
            "project_id": str(project.id),
            "title": task.title,
            "status": task.status.value,
            "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        }
        await self._tasks.delete(task)

        await self._audit.record(
            actor=actor,
            action=AuditAction.TASK_DELETED,
            entity_type=AuditEntity.TASK,
            entity_id=task.id,
            details=details,
        )

    async def _ensure_assignable(self, *, project: Project, assignee_id: uuid.UUID | None) -> None:
        """Check that a nominated assignee belongs to the project."""
        membership = (
            await self._projects.get_membership(project_id=project.id, user_id=assignee_id)
            if assignee_id is not None
            else None
        )
        rules.ensure_assignee_is_member(assignee_id=assignee_id, membership=membership)
