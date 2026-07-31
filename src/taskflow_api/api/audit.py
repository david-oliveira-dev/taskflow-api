"""The audit trail, read-only.

There is no POST, PATCH or DELETE here, and there never will be. Entries are written as a
side effect of the mutations they describe; an endpoint that let anyone append to the trail
directly would make every entry unfalsifiable.

Restricted to global administrators. The trail records who did what across every project, so
exposing it to ordinary users would leak the existence and membership of projects they have
no access to — the same disclosure the 404-for-non-members rule exists to prevent.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from taskflow_api.api.deps import PageParamsDep, SessionDep, require_global_role
from taskflow_api.api.routing import TransactionalRoute
from taskflow_api.api.schemas import AuditEntryResponse, PageResponse
from taskflow_api.enums import AuditEntity, GlobalRole
from taskflow_api.services.audit import AuditService

router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(require_global_role(GlobalRole.ADMIN))],
    route_class=TransactionalRoute,
)


@router.get("", summary="Read the audit trail (admin only)")
async def list_audit(
    session: SessionDep,
    page: PageParamsDep,
    entity_type: Annotated[AuditEntity | None, Query(description="Filter by entity kind.")] = None,
    entity_id: Annotated[uuid.UUID | None, Query(description="Filter by entity id.")] = None,
) -> PageResponse[AuditEntryResponse]:
    """List audit entries, most recent first.

    The two filters together match `ix_audit_log_entity`, which is what makes "everything
    that happened to this project" cheap on a table that only ever grows.
    """
    result = await AuditService(session).list_entries(
        limit=page.limit, after=page.after, entity_type=entity_type, entity_id=entity_id
    )
    return PageResponse(
        items=[AuditEntryResponse.of(entry) for entry in result.items],
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )
