"""Pydantic schemas — the wire format.

These are the **API** shapes. ORM models live in `db/models.py`, and the two are kept
separate on purpose: a schema that is just the ORM row serialised leaks columns the client
should never see, `hashed_password` first among them.
"""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from taskflow_api.db.models import AuditLog, Membership, User
from taskflow_api.enums import GlobalRole, ProjectRole, TaskStatus


class Request(BaseModel):
    """Base for anything parsed from a request body.

    `extra="forbid"` so a misspelled field is a 422 instead of a silently ignored one. A
    client that sends `nmae` and gets a 200 back has no way to discover the mistake.
    """

    model_config = ConfigDict(extra="forbid")


class PageResponse[T](BaseModel):
    """The single envelope every listing returns.

    One shape across all listings means a client writes the paging loop once. `has_more` is
    reported separately from `next_cursor` being set so the contract stays explicit rather
    than something callers infer from a null.
    """

    items: list[T]
    next_cursor: str | None = None
    has_more: bool


class RegisterRequest(Request):
    """Payload for creating an account."""

    email: EmailStr
    # Long minimum, no composition rules. Length is what actually resists guessing; forcing
    # a symbol and a digit mostly produces "Password1!" and a note on a monitor.
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class UserResponse(BaseModel):
    """A user as the API exposes it.

    Note what is absent: `hashed_password` is not here, and cannot be added by accident,
    because this is not built from the ORM row by reflection.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    global_role: GlobalRole
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """An issued access token, in the shape OAuth2 clients expect."""

    access_token: str
    # S105 fires on the name, not the value: this is the OAuth2 token *type*, a literal
    # the RFC mandates, not a secret.
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(description="Seconds until the token expires.")


class ProjectCreateRequest(Request):
    """Payload for creating a project."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)


class ProjectUpdateRequest(Request):
    """Payload for a partial project update.

    Every field is optional, so `model_dump(exclude_unset=True)` is what the service
    receives — that is the only way to tell "leave the description alone" apart from "clear
    the description", which are different requests that would otherwise look identical.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> Self:
        """Reject an empty patch.

        A PATCH with no fields is a client bug. Answering 200 to it hides the bug behind a
        response that looks like success.
        """
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self


class ProjectResponse(BaseModel):
    """A project as the API exposes it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    is_archived: bool
    created_at: datetime


class MemberAddRequest(Request):
    """Payload for granting someone a role in a project."""

    user_id: uuid.UUID
    project_role: ProjectRole


class MemberRoleUpdateRequest(Request):
    """Payload for changing an existing member's role."""

    project_role: ProjectRole


class MemberResponse(BaseModel):
    """A project member, with enough of the account to be useful.

    Built explicitly from the two objects rather than by reflection over the membership row:
    reading `membership.user` on a row whose relationship was not eagerly loaded raises
    under asyncio, and doing it during serialisation makes that failure surface far from its
    cause.
    """

    user_id: uuid.UUID
    email: EmailStr
    full_name: str
    project_role: ProjectRole
    created_at: datetime

    @classmethod
    def of(cls, membership: Membership, user: User) -> "MemberResponse":
        """Build a response from a membership and its user."""
        return cls(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            project_role=membership.project_role,
            created_at=membership.created_at,
        )


class TaskCreateRequest(Request):
    """Payload for adding a task to a project."""

    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    status: TaskStatus = TaskStatus.TODO
    # Bounded here as well as by the database CHECK. The constraint is the guarantee; this
    # is what turns a violation into a 422 that names the field instead of a 500.
    priority: int = Field(default=3, ge=1, le=5)
    assignee_id: uuid.UUID | None = None


class TaskUpdateRequest(Request):
    """Payload for a partial task update.

    As with projects, every field is optional and the service receives
    `model_dump(exclude_unset=True)`: omitting `assignee_id` leaves it alone, sending it as
    null unassigns the task, and those are different requests.
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    status: TaskStatus | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    assignee_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> Self:
        """Reject an empty patch."""
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self


class TaskResponse(BaseModel):
    """A task as the API exposes it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    priority: int
    assignee_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AuditEntryResponse(BaseModel):
    """One entry of the audit trail.

    `actor_email` is present even when `actor_id` is null — that pairing is the whole point
    of denormalising the address at write time.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_email: str
    action: str
    entity_type: str
    entity_id: uuid.UUID
    at: datetime
    details: dict[str, object]

    @classmethod
    def of(cls, entry: AuditLog) -> "AuditEntryResponse":
        """Build a response from an audit row."""
        return cls.model_validate(entry)
