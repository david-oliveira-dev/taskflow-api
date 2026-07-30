"""Data access for users.

Repositories issue queries and return ORM objects. They never commit — the transaction
boundary belongs to the caller, so a service can write an entity and its audit entry in one
atomic unit (see `db.session.session_scope`).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.db.models import User
from taskflow_api.enums import GlobalRole
from taskflow_api.exceptions import ConflictError


class UserRepository:
    """Reads and writes `User` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        email: str,
        hashed_password: str,
        full_name: str,
        global_role: GlobalRole = GlobalRole.USER,
    ) -> User:
        """Insert a user.

        Args:
            email: Unique address. Stored and matched case-insensitively by lowering it here.
            hashed_password: Already hashed. This layer never sees a plaintext password.
            full_name: Display name.
            global_role: Account-wide role.

        Returns:
            The persisted user, with its generated id.

        Raises:
            ConflictError: The email is already registered.
        """
        user = User(
            email=email.lower(),
            hashed_password=hashed_password,
            full_name=full_name,
            global_role=global_role,
        )
        # A SAVEPOINT, not a bare flush. In PostgreSQL a failed statement aborts the whole
        # transaction, so recovering from the unique violation without one would leave the
        # session unusable — every later statement would fail with "current transaction is
        # aborted". The savepoint confines the damage to this insert, which is what lets a
        # caller catch ConflictError and carry on inside the same transaction.
        try:
            async with self._session.begin_nested():
                self._session.add(user)
                await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"Email already registered: {email}") from exc
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        """Return a user by id, or None."""
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Return a user by email, or None.

        Lowercased before matching so that `Ada@example.com` and `ada@example.com` are the
        same account — otherwise the unique constraint is trivially bypassed at signup.
        """
        result = await self._session.execute(select(User).where(User.email == email.lower()))
        return result.scalar_one_or_none()
