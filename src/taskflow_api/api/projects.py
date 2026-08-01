"""Project and membership endpoints.

Authorisation is declared per route through `require_project_role`, so the required role is
visible in the signature instead of buried in the body. The split follows the spec: managing
membership is an owner's job, editing tasks will be an editor's, and a viewer only reads.

Handlers stay thin — parse, delegate, serialise. Domain failures propagate as domain errors
and are turned into statuses by `api/errors.py`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from taskflow_api.api.deps import (
    CurrentUserDep,
    PageParamsDep,
    ProjectAccess,
    SessionDep,
    require_project_role,
)
from taskflow_api.api.idempotency import IdempotencyDep, IdempotentRoute
from taskflow_api.api.schemas import (
    MemberAddRequest,
    MemberResponse,
    MemberRoleUpdateRequest,
    PageResponse,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
)
from taskflow_api.enums import ProjectRole
from taskflow_api.services.projects import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"], route_class=IdempotentRoute)

ViewerAccess = Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.VIEWER))]
OwnerAccess = Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.OWNER))]


@router.get("", summary="List the projects you belong to")
async def list_projects(
    user: CurrentUserDep,
    session: SessionDep,
    page: PageParamsDep,
    include_archived: Annotated[bool, Query(description="Include archived projects.")] = False,
) -> PageResponse[ProjectResponse]:
    """List the caller's projects, newest first.

    Archived projects are hidden by default: they are this service's deleted projects, and
    a listing that shows them by default makes archiving feel like it did nothing.
    """
    result = await ProjectService(session).list_for_user(
        user=user, limit=page.limit, after=page.after, include_archived=include_archived
    )
    return PageResponse(
        items=[ProjectResponse.model_validate(p) for p in result.items],
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
    dependencies=[IdempotencyDep],
)
async def create_project(
    payload: ProjectCreateRequest, user: CurrentUserDep, session: SessionDep
) -> ProjectResponse:
    """Create a project. The caller becomes its owner."""
    project = await ProjectService(session).create(
        actor=user, name=payload.name, description=payload.description
    )
    return ProjectResponse.model_validate(project)


@router.get("/{project_id}", summary="Read a project")
async def get_project(access: ViewerAccess) -> ProjectResponse:
    """Return one project. Archived projects are still readable by id."""
    return ProjectResponse.model_validate(access.project)


@router.patch("/{project_id}", summary="Update a project")
async def update_project(
    payload: ProjectUpdateRequest, access: OwnerAccess, session: SessionDep
) -> ProjectResponse:
    """Apply a partial update.

    `exclude_unset` is what preserves the difference between omitting `description` and
    sending it as null — the first means "leave it", the second means "clear it".
    """
    project = await ProjectService(session).update(
        actor=access.user,
        project=access.project,
        changes=payload.model_dump(exclude_unset=True),
    )
    return ProjectResponse.model_validate(project)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a project (soft delete)",
)
async def archive_project(access: OwnerAccess, session: SessionDep) -> Response:
    """Archive a project.

    There is no physical delete. The project stops appearing in listings and refuses every
    further mutation, but its tasks and its audit trail stay intact.
    """
    await ProjectService(session).archive(actor=access.user, project=access.project)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/members", summary="List a project's members")
async def list_members(
    access: ViewerAccess, session: SessionDep, page: PageParamsDep
) -> PageResponse[MemberResponse]:
    """List the project's members, newest first."""
    result = await ProjectService(session).list_members(
        project=access.project, limit=page.limit, after=page.after
    )
    return PageResponse(
        items=[MemberResponse.of(m, m.user) for m in result.items],
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.post(
    "/{project_id}/members",
    status_code=status.HTTP_201_CREATED,
    summary="Add a member",
    dependencies=[IdempotencyDep],
)
async def add_member(
    payload: MemberAddRequest, access: OwnerAccess, session: SessionDep
) -> MemberResponse:
    """Grant a user a role in the project."""
    membership, member = await ProjectService(session).add_member(
        actor=access.user,
        project=access.project,
        user_id=payload.user_id,
        role=payload.project_role,
    )
    return MemberResponse.of(membership, member)


@router.patch("/{project_id}/members/{user_id}", summary="Change a member's role")
async def change_member_role(
    user_id: uuid.UUID,
    payload: MemberRoleUpdateRequest,
    access: OwnerAccess,
    session: SessionDep,
) -> MemberResponse:
    """Change an existing member's role.

    Demoting the project's only owner is refused: it would leave nobody able to appoint a
    replacement.
    """
    membership, member = await ProjectService(session).change_member_role(
        actor=access.user,
        project=access.project,
        user_id=user_id,
        role=payload.project_role,
    )
    return MemberResponse.of(membership, member)


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
)
async def remove_member(user_id: uuid.UUID, access: OwnerAccess, session: SessionDep) -> Response:
    """Remove a member.

    Their tasks in this project stay and lose their assignee; how many is recorded in the
    audit entry. Removing the only owner is refused.
    """
    await ProjectService(session).remove_member(
        actor=access.user, project=access.project, user_id=user_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
