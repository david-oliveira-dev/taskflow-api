"""Registration and authentication.

Business rules live here. This module knows about repositories and domain errors; it does
not know what an HTTP status code is, which is what keeps it testable without a client.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.config import Settings
from taskflow_api.db.models import User
from taskflow_api.exceptions import AuthenticationError
from taskflow_api.repositories.users import UserRepository
from taskflow_api.services import security


class AuthService:
    """Creates accounts and verifies credentials."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._users = UserRepository(session)
        self._settings = settings

    async def register(self, *, email: str, password: str, full_name: str) -> User:
        """Create an account.

        Raises:
            ConflictError: The email is already registered. Propagated from the repository.
        """
        return await self._users.create(
            email=email,
            hashed_password=security.hash_password(password),
            full_name=full_name,
        )

    async def authenticate(self, *, email: str, password: str) -> User:
        """Verify credentials and return the user.

        Every failure — unknown email, wrong password, deactivated account — raises the
        same error with the same message. Distinguishing them would tell an attacker which
        addresses are registered, and `verify_password` already equalises the timing.

        Raises:
            AuthenticationError: The credentials are not valid for an active account.
        """
        user = await self._users.get_by_email(email)
        stored_hash = user.hashed_password if user is not None else None

        # Runs even when `user` is None, against a dummy hash, so the two paths cost the
        # same. Short-circuiting here would reintroduce the timing side channel.
        password_ok = security.verify_password(password, stored_hash)

        if user is None or not password_ok or not user.is_active:
            raise AuthenticationError("Incorrect email or password.")

        # Transparently upgrade a hash made with older, weaker parameters. This is the one
        # moment the plaintext is available, so it is the only place it can happen.
        if security.needs_rehash(user.hashed_password):
            user.hashed_password = security.hash_password(password)

        return user

    def issue_token(self, user: User) -> tuple[str, int]:
        """Mint an access token for a user.

        Returns:
            The encoded token and its lifetime in seconds.
        """
        token = security.create_access_token(user_id=user.id, settings=self._settings)
        return token, self._settings.access_token_ttl_minutes * 60
