"""Task endpoints.

The paths are split deliberately. Creating and listing tasks is addressed through the
project that owns them, because that is the collection being read or added to. A task that
already exists is addressed directly at `/tasks/{task_id}`, so a client holding a task id
does not have to remember which project it came from.

Authorisation follows the roles the specification lays out: an editor creates and edits
tasks but never touches membership, and a viewer only reads.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from taskflow_api.api.deps import (
    PageParamsDep,
    ProjectAccess,
    SessionDep,
    TaskAccess,
    require_project_role,
    require_task_role,
)
from taskflow_api.api.idempotency import IdempotencyDep, IdempotentRoute
from taskflow_api.api.schemas import (
    PageResponse,
    TaskCreateRequest,
    TaskResponse,
    TaskUpdateRequest,
)
from taskflow_api.enums import ProjectRole, TaskStatus
from taskflow_api.services.tasks import TaskService

router = APIRouter(tags=["tasks"], route_class=IdempotentRoute)

ProjectViewer = Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.VIEWER))]
ProjectEditor = Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.EDITOR))]
TaskViewer = Annotated[TaskAccess, Depends(require_task_role(ProjectRole.VIEWER))]
TaskEditor = Annotated[TaskAccess, Depends(require_task_role(ProjectRole.EDITOR))]


@router.get("/projects/{project_id}/tasks", summary="List a project's tasks")
async def list_tasks(
    access: ProjectViewer,
    session: SessionDep,
    page: PageParamsDep,
    status_filter: Annotated[
        TaskStatus | None, Query(alias="status", description="Only tasks in this state.")
    ] = None,
) -> PageResponse[TaskResponse]:
    """List the project's tasks, newest first.

    The query parameter is `status`; the Python name differs only because `status` is
    already the imported FastAPI module in this file.
    """
    result = await TaskService(session).list_for_project(
        project=access.project, limit=page.limit, after=page.after, status=status_filter
    )
    return PageResponse(
        items=[TaskResponse.model_validate(task) for task in result.items],
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.post(
    "/projects/{project_id}/tasks",
    status_code=status.HTTP_201_CREATED,
    summary="Add a task to a project",
    dependencies=[IdempotencyDep],
)
async def create_task(
    payload: TaskCreateRequest, access: ProjectEditor, session: SessionDep
) -> TaskResponse:
    """Create a task.

    The assignee, if given, must already be a member of the project — assigning work to
    someone who cannot open it is refused with 422.
    """
    task = await TaskService(session).create(
        actor=access.user,
        project=access.project,
        title=payload.title,
        description=payload.description,
        status=payload.status,
        priority=payload.priority,
        assignee_id=payload.assignee_id,
    )
    return TaskResponse.model_validate(task)


@router.get("/tasks/{task_id}", summary="Read a task")
async def get_task(access: TaskViewer) -> TaskResponse:
    """Return one task."""
    return TaskResponse.model_validate(access.task)


@router.patch("/tasks/{task_id}", summary="Update a task")
async def update_task(
    payload: TaskUpdateRequest, access: TaskEditor, session: SessionDep
) -> TaskResponse:
    """Apply a partial update.

    Sending `assignee_id` as null unassigns the task; omitting it leaves the assignee
    alone.
    """
    task = await TaskService(session).update(
        actor=access.user,
        project=access.project,
        task=access.task,
        changes=payload.model_dump(exclude_unset=True),
    )
    return TaskResponse.model_validate(task)


@router.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task",
)
async def delete_task(access: TaskEditor, session: SessionDep) -> Response:
    """Delete a task.

    Unlike a project, a task is removed for real. It is cheap to recreate and there is
    nothing to archive; the audit entry keeps its title so the trail still says what went.
    """
    await TaskService(session).delete(actor=access.user, project=access.project, task=access.task)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
