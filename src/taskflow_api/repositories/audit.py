"""Data access for the audit trail.

Note what this class does **not** have: no update, no delete, no upsert. The trail is
append-only, and the cheapest way to guarantee that is to give the application no vocabulary
for expressing anything else. A rule enforced by absence cannot be forgotten in review.
"""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from taskflow_api.db.models import AuditLog
from taskflow_api.enums import AuditAction, AuditEntity
from taskflow_api.pagination import CursorPosition, Page
from taskflow_api.repositories.base import after_position, paginate


def _position(entry: AuditLog) -> CursorPosition:
    return CursorPosition(created_at=entry.at, entity_id=entry.id)


class AuditRepository:
    """Appends to, and reads from, `audit_log`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_email: str,
        action: AuditAction,
        entity_type: AuditEntity,
        entity_id: uuid.UUID,
        details: dict[str, object],
    ) -> AuditLog:
        """Append one entry.

        No commit here: the entry has to land in the same transaction as the mutation it
        describes. An audit row that survives a rolled-back change is a lie, and one that is
        lost when the change succeeds is worse.

        Args:
            actor_id: Who acted, or None once that account is gone.
            actor_email: Denormalised at write time so the entry outlives the account.
            action: What was done.
            entity_type: What kind of thing it was done to.
            entity_id: Which one.
            details: Free-form context, stored as JSONB.

        Returns:
            The appended entry.
        """
        entry = AuditLog(
            actor_id=actor_id,
            actor_email=actor_email,
            action=action.value,
            entity_type=entity_type.value,
            entity_id=entity_id,
            details=details,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def list_entries(
        self,
        *,
        limit: int,
        after: CursorPosition | None = None,
        entity_type: AuditEntity | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> Page[AuditLog]:
        """List entries, most recent first.

        Args:
            limit: Page size, already clamped by the caller.
            after: Continue after this position.
            entity_type: Restrict to one kind of entity.
            entity_id: Restrict to one entity. Uses `ix_audit_log_entity` together with
                `entity_type`, which is why both filters exist rather than just the id.

        Returns:
            A page of entries.
        """
        stmt: Select[tuple[AuditLog]] = select(AuditLog).order_by(
            AuditLog.at.desc(), AuditLog.id.desc()
        )
        if entity_type is not None:
            stmt = stmt.where(AuditLog.entity_type == entity_type.value)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if after is not None:
            stmt = stmt.where(after_position(AuditLog.at, AuditLog.id, after))

        return await paginate(self._session, stmt, limit=limit, position=_position)
