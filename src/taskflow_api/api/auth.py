"""Authentication endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from taskflow_api.api.deps import CurrentUserDep, SessionDep, SettingsDep
from taskflow_api.api.schemas import RegisterRequest, TokenResponse, UserResponse
from taskflow_api.exceptions import AuthenticationError, ConflictError
from taskflow_api.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(
    payload: RegisterRequest, session: SessionDep, settings: SettingsDep
) -> UserResponse:
    """Register a new user.

    Returns 409 when the email is taken. That does disclose whether an address is
    registered — unavoidable for a public signup form, since the user has to be told why it
    failed. The login endpoint, where it would be exploitable at scale, does not.
    """
    service = AuthService(session, settings)
    try:
        user = await service.register(
            email=payload.email, password=payload.password, full_name=payload.full_name
        )
    except ConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered."
        ) from exc

    return UserResponse.model_validate(user)


@router.post("/login", summary="Exchange credentials for an access token")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    settings: SettingsDep,
) -> TokenResponse:
    """Authenticate and issue a token.

    The form field is called `username` because that is what the OAuth2 password flow
    specifies; this service puts an email in it.

    Unknown address, wrong password and deactivated account all produce the same 401 with
    the same message, and take the same time. Anything else turns this endpoint into a way
    to find out which addresses have accounts.
    """
    service = AuthService(session, settings)
    try:
        user = await service.authenticate(email=form.username, password=form.password)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    token, expires_in = service.issue_token(user)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", summary="The authenticated account")
async def me(user: CurrentUserDep) -> UserResponse:
    """Return the caller's own account."""
    return UserResponse.model_validate(user)
