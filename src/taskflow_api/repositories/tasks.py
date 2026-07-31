"""Data access for tasks."""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.db.models import Task
from taskflow_api.enums import TaskStatus
from taskflow_api.pagination import CursorPosition, Page
from taskflow_api.repositories.base import after_position, paginate


def _position(task: Task) -> CursorPosition:
    return CursorPosition(created_at=task.created_at, entity_id=task.id)


class TaskRepository:
    """Reads and writes `Task` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        title: str,
        description: str | None = None,
        status: TaskStatus = TaskStatus.TODO,
        priority: int = 3,
        assignee_id: uuid.UUID | None = None,
    ) -> Task:
        """Insert a task.

        Whether `assignee_id` is a member of the project is a business rule, enforced in the
        service layer where the membership is already loaded — not here, where it would mean
        a second query on every write.
        """
        task = Task(
            project_id=project_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee_id=assignee_id,
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def get(self, task_id: uuid.UUID) -> Task | None:
        """Return a task by id, or None."""
        return await self._session.get(Task, task_id)

    async def delete(self, task: Task) -> None:
        """Remove a task for good.

        Tasks are the one entity deleted physically rather than flagged. They are cheap,
        there is nothing to archive, and the audit entry keeps the record of what was
        removed — which is why the service writes the title into it before calling this.
        """
        await self._session.delete(task)
        await self._session.flush()

    async def unassign_all_for_user(self, *, project_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """Clear the assignee on every task in a project assigned to a user.

        Removing someone from a project must not delete their work, so the tasks stay and
        lose their assignee. Returns how many tasks were affected, which the audit entry
        records.
        """
        result = await self._session.execute(
            select(Task).where(Task.project_id == project_id, Task.assignee_id == user_id)
        )
        tasks = list(result.scalars().all())
        for task in tasks:
            task.assignee_id = None
        await self._session.flush()
        return len(tasks)

    async def list_for_project(
        self,
        *,
        project_id: uuid.UUID,
        limit: int,
        after: CursorPosition | None = None,
        status: TaskStatus | None = None,
    ) -> Page[Task]:
        """List a project's tasks, newest first, optionally filtered by status."""
        stmt: Select[tuple[Task]] = (
            select(Task)
            .where(Task.project_id == project_id)
            .order_by(Task.created_at.desc(), Task.id.desc())
        )
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if after is not None:
            stmt = stmt.where(after_position(Task.created_at, Task.id, after))

        return await paginate(self._session, stmt, limit=limit, position=_position)
