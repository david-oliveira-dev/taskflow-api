"""Writing and reading the audit trail.

The service exists mainly to make one thing impossible to forget: `actor_email` is copied
from the actor at write time. `actor_id` alone would be enough right up until the account is
deleted, at which point the foreign key goes NULL and the entry stops saying who acted. A
trail that forgets its author under exactly the circumstances where it matters most is not a
trail.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.db.models import AuditLog, User
from taskflow_api.enums import AuditAction, AuditEntity
from taskflow_api.pagination import CursorPosition, Page
from taskflow_api.repositories.audit import AuditRepository


class AuditService:
    """Records mutations and serves the trail."""

    def __init__(self, session: AsyncSession) -> None:
        self._entries = AuditRepository(session)

    async def record(
        self,
        *,
        actor: User,
        action: AuditAction,
        entity_type: AuditEntity,
        entity_id: uuid.UUID,
        details: dict[str, object] | None = None,
    ) -> AuditLog:
        """Append an entry describing what the actor just did.

        Called inside the caller's transaction, never after it. The mutation and its record
        commit together or not at all.

        Args:
            actor: Who performed the mutation.
            action: What they did.
            entity_type: What kind of thing they did it to.
            entity_id: Which one.
            details: Context worth keeping — the fields that changed, counts of side
                effects. Stored as JSONB, so it stays queryable.

        Returns:
            The appended entry.
        """
        return await self._entries.record(
            actor_id=actor.id,
            actor_email=actor.email,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )

    async def list_entries(
        self,
        *,
        limit: int,
        after: CursorPosition | None = None,
        entity_type: AuditEntity | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> Page[AuditLog]:
        """List trail entries, most recent first."""
        return await self._entries.list_entries(
            limit=limit, after=after, entity_type=entity_type, entity_id=entity_id
        )
