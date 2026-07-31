"""Request-scoped dependencies: configuration, session, identity and authorisation."""

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Path, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskflow_api.config import Settings
from taskflow_api.db.models import Membership, Project, Task, User
from taskflow_api.enums import GlobalRole, ProjectRole
from taskflow_api.exceptions import AuthenticationError, InvalidCursorError
from taskflow_api.pagination import CursorPosition, clamp_page_size, decode_cursor
from taskflow_api.repositories.projects import ProjectRepository
from taskflow_api.repositories.tasks import TaskRepository
from taskflow_api.repositories.users import UserRepository
from taskflow_api.services import security
from taskflow_api.services.rules import effective_role

# `auto_error=False` so a missing header reaches our own handler and produces the project's
# error shape, instead of Starlette's default body.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def get_settings(request: Request) -> Settings:
    """Return the settings built at startup."""
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction is committed on success, rolled back on failure.

    The boundary is the request, not the repository call: a handler that writes an entity
    and its audit entry must have both or neither.

    The session is published on `request.state` so that `TransactionalRoute` can commit it
    while the response still exists but has not been sent. The commit below stays as a
    backstop for any route not built on that class — arriving late is recoverable, never
    arriving is not.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        request.state.session = session
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
TokenDep = Annotated[str | None, Depends(oauth2_scheme)]


def _unauthorised(detail: str) -> HTTPException:
    # WWW-Authenticate is required by RFC 6750 on a 401 from a bearer-protected resource,
    # and it is what tells a client to re-authenticate rather than give up.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(token: TokenDep, session: SessionDep, settings: SettingsDep) -> User:
    """Resolve the caller from the bearer token.

    Raises:
        HTTPException: 401 when the token is absent, invalid, expired, or belongs to an
            account that no longer exists or has been deactivated.
    """
    if token is None:
        raise _unauthorised("Not authenticated.")

    try:
        payload = security.decode_access_token(token, settings=settings)
    except AuthenticationError as exc:
        raise _unauthorised(str(exc)) from exc

    user = await UserRepository(session).get(payload.subject)
    if user is None:
        # A valid signature over a user that has since been deleted.
        raise _unauthorised("Account no longer exists.")
    if not user.is_active:
        # Deactivation has to be checked on every request. Tokens are not revocable, so a
        # token minted before the account was disabled is still cryptographically valid —
        # this check is the only thing standing between it and a live session.
        raise _unauthorised("Account is deactivated.")

    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_global_role(
    required: GlobalRole,
) -> Callable[[User], Coroutine[Any, Any, User]]:
    """Build a dependency that demands an account-wide role.

    Args:
        required: The role the caller must hold.

    Returns:
        A dependency returning the user, or raising 403.
    """

    async def dependency(user: CurrentUserDep) -> User:
        if user.global_role is not required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This operation requires the {required} role.",
            )
        return user

    return dependency


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    """The outcome of authorising a caller against one project.

    Carries the project itself so handlers do not fetch it a second time — the dependency
    has already loaded it in order to decide whether the caller may proceed.
    """

    project: Project
    user: User
    #: None when a global admin reaches the project without being a member.
    membership: Membership | None
    role: ProjectRole


def require_project_role(
    required: ProjectRole,
) -> Callable[..., Coroutine[Any, Any, ProjectAccess]]:
    """Build a dependency that demands a role inside the project named in the path.

    Archived projects still pass: they stay readable by id, and refusing writes to them is
    the service layer's job, where the message can say why.

    Args:
        required: The minimum project role, using the ordering in `ProjectRole`.

    Returns:
        A dependency returning the established access, or raising 404/403.
    """

    async def dependency(
        project_id: Annotated[uuid.UUID, Path()],
        user: CurrentUserDep,
        session: SessionDep,
    ) -> ProjectAccess:
        repo = ProjectRepository(session)

        project = await repo.get(project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

        membership = await repo.get_membership(project_id=project_id, user_id=user.id)
        role = effective_role(
            user=user,
            membership_role=membership.project_role if membership is not None else None,
        )
        if role is None:
            # 404, not 403. Telling a stranger "you lack permission" confirms the project
            # exists, which turns id probing into a directory of everyone's projects.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

        if not role.can_act_as(required):
            # 403 here is fine: the caller already knows the project exists.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This operation requires the {required} role in this project.",
            )

        return ProjectAccess(project=project, user=user, membership=membership, role=role)

    return dependency


@dataclass(frozen=True, slots=True)
class TaskAccess:
    """The outcome of authorising a caller against one task.

    Carries the task's project because every task rule needs it — whether the project is
    archived, and whether a nominated assignee belongs to it.
    """

    task: Task
    project: Project
    user: User
    membership: Membership | None
    role: ProjectRole


def _task_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")


def require_task_role(required: ProjectRole) -> Callable[..., Coroutine[Any, Any, TaskAccess]]:
    """Build a dependency that demands a role in the project owning the task in the path.

    `/tasks/{task_id}` carries no project id, so authorisation has to go through the task to
    reach the membership. That is why this exists next to `require_project_role` instead of
    reusing it.

    Args:
        required: The minimum project role.

    Returns:
        A dependency returning the established access, or raising 404/403.
    """

    async def dependency(
        task_id: Annotated[uuid.UUID, Path()],
        user: CurrentUserDep,
        session: SessionDep,
    ) -> TaskAccess:
        task = await TaskRepository(session).get(task_id)
        if task is None:
            raise _task_not_found()

        projects = ProjectRepository(session)
        project = await projects.get(task.project_id)
        if project is None:  # pragma: no cover - the foreign key makes this unreachable
            raise _task_not_found()

        membership = await projects.get_membership(project_id=project.id, user_id=user.id)
        role = effective_role(
            user=user,
            membership_role=membership.project_role if membership is not None else None,
        )
        if role is None:
            # 404 for the same reason as on projects: a 403 would confirm this task exists,
            # letting anyone map out other people's work by probing ids.
            raise _task_not_found()

        if not role.can_act_as(required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This operation requires the {required} role in this project.",
            )

        return TaskAccess(task=task, project=project, user=user, membership=membership, role=role)

    return dependency


@dataclass(frozen=True, slots=True)
class PageParams:
    """A validated request for one page of a listing."""

    limit: int
    after: CursorPosition | None


def page_params(
    cursor: Annotated[str | None, Query(description="Opaque token from a previous page.")] = None,
    limit: Annotated[int | None, Query(ge=1, description="Page size.")] = None,
) -> PageParams:
    """Parse and bound the paging arguments.

    Cursors come straight from the query string, so a malformed one is ordinary user input
    and has to produce a 422 rather than an unhandled exception. The size is clamped rather
    than rejected: an unbounded page is a denial-of-service vector that costs the database
    far more than it costs the caller.

    Raises:
        HTTPException: 422 when the cursor cannot be parsed.
    """
    try:
        after = decode_cursor(cursor) if cursor else None
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return PageParams(limit=clamp_page_size(limit), after=after)


PageParamsDep = Annotated[PageParams, Depends(page_params)]
