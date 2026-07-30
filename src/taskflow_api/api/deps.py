"""Request-scoped dependencies: configuration, session, identity and authorisation."""

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskflow_api.config import Settings
from taskflow_api.db.models import Membership, User
from taskflow_api.enums import GlobalRole, ProjectRole
from taskflow_api.exceptions import AuthenticationError
from taskflow_api.repositories.projects import ProjectRepository
from taskflow_api.repositories.users import UserRepository
from taskflow_api.services import security

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
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
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


def require_project_role(
    required: ProjectRole,
) -> Callable[..., Coroutine[Any, Any, Membership]]:
    """Build a dependency that demands a role inside the project named in the path.

    Args:
        required: The minimum project role, using the ordering in `ProjectRole`.

    Returns:
        A dependency returning the caller's membership, or raising 404/403.
    """

    async def dependency(
        project_id: Annotated[uuid.UUID, Path()],
        user: CurrentUserDep,
        session: SessionDep,
    ) -> Membership:
        repo = ProjectRepository(session)

        if await repo.get(project_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

        membership = await repo.get_membership(project_id=project_id, user_id=user.id)
        if membership is None:
            # 404, not 403. Telling a stranger "you lack permission" confirms the project
            # exists, which turns id probing into a directory of everyone's projects.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

        if not membership.project_role.can_act_as(required):
            # 403 here is fine: the caller already knows the project exists.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This operation requires the {required} role in this project.",
            )

        return membership

    return dependency
