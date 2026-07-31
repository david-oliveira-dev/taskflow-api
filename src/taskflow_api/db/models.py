"""ORM models.

Naming, so the usual FastAPI confusion never starts: this module holds **ORM** models;
Pydantic schemas live in `api/schemas.py`. The bare word "model" is not used anywhere.

Two conventions apply throughout:

- **UUID primary keys.** Sequential integers let anyone walk the API by incrementing an id
  and turn a 403 into a census of what exists. UUIDs cost a little space and remove that.
- **Enums as VARCHAR with a CHECK constraint** (`native_enum=False`) rather than PostgreSQL
  ENUM types. Adding a value to a native enum needs `ALTER TYPE`, which cannot run inside a
  transaction on older servers and makes downgrades genuinely painful; a CHECK constraint is
  just another constraint to Alembic. Both halves of that need saying out loud, because
  SQLAlchemy's defaults give you neither — see `_enum_column`.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from taskflow_api.db.base import Base
from taskflow_api.enums import GlobalRole, IdempotencyStatus, ProjectRole, TaskStatus


def _enum_column(enum_type: type[StrEnum], *, name: str) -> Enum:
    """A VARCHAR column holding an enum's **values**, guarded by a CHECK constraint.

    Both keyword arguments override a SQLAlchemy default that is wrong for this service:

    - `values_callable` — by default SQLAlchemy persists the member *name*, so the database
      would hold `OWNER` and `TODO` while the API speaks `owner` and `todo`. Everything
      round-trips, which is exactly why it survives testing; what breaks is every consumer
      that is not this application. `SELECT ... WHERE status = 'done'` from psql, a report,
      or a BI tool quietly matches nothing. For a service whose whole argument is an
      auditable trail, the column has to read the way the API reads.
    - `create_constraint` — defaults to False since SQLAlchemy 1.4, so `native_enum=False`
      on its own produces a bare VARCHAR that accepts any string at all. The constraint this
      module's docstring promises has to be asked for explicitly.

    Args:
        enum_type: The Python enum whose values the column accepts.
        name: Constraint name, combined with the table name by the naming convention.

    Returns:
        The configured column type.
    """
    return Enum(
        enum_type,
        native_enum=False,
        create_constraint=True,
        length=16,
        name=name,
        values_callable=lambda enum: [member.value for member in enum],
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    """A UUID primary key generated on the Python side."""
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    """Creation timestamp, timezone-aware, defaulted by the database."""
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    """An account."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    global_role: Mapped[GlobalRole] = mapped_column(
        _enum_column(GlobalRole, name="global_role"),
        default=GlobalRole.USER,
        nullable=False,
    )
    # Users are never physically deleted (see the deletion policy in the spec): this flag is
    # the soft delete, and it also blocks authentication for an existing token.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Project(Base):
    """A container of tasks, with its own membership list."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # RESTRICT rather than CASCADE: users are soft-deleted, so a physical delete that would
    # take projects with it is a mistake, and the database should say so.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Supports the default listing, which hides archived projects and pages by cursor.
        Index("ix_projects_active_created_at", "is_archived", "created_at", "id"),
    )


class Membership(Base):
    """A user's role inside one project.

    The composite primary key is the constraint that makes "a user belongs to a project at
    most once" a database fact rather than something the service has to remember to check.
    """

    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    project_role: Mapped[ProjectRole] = mapped_column(
        _enum_column(ProjectRole, name="project_role"), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()

    user: Mapped[User] = relationship(back_populates="memberships")
    project: Mapped[Project] = relationship(back_populates="memberships")


class Task(Base):
    """A unit of work inside a project."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        _enum_column(TaskStatus, name="task_status"),
        default=TaskStatus.TODO,
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # SET NULL, not CASCADE: removing someone from a project must not delete the work.
    # The business rule that an assignee has to be a member lives in the service layer.
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="tasks")

    __table_args__ = (
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority_range"),
        Index("ix_tasks_project_created_at", "project_id", "created_at", "id"),
        Index("ix_tasks_project_status", "project_id", "status"),
    )


class AuditLog(Base):
    """An append-only record of a mutation.

    Never updated, never deleted — not by the application and not through any endpoint.

    Two details make the trail survive its subject. `actor_id` is `SET NULL` so deleting a
    user cannot cascade the history away, and `actor_email` is denormalised at write time so
    the entry still says *who* did it after the account is gone. A trail that loses its
    author is not a trail.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    at: Mapped[datetime] = _created_at()
    # Named `details`, not `metadata`: `metadata` is reserved on the declarative base and
    # raises InvalidRequestError. The database column keeps the descriptive name.
    details: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False)

    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_at", "at", "id"),
    )


class IdempotencyKey(Base):
    """A recorded `Idempotency-Key`, with the response it produced.

    The composite primary key is the mechanism, not just a constraint: serialising
    concurrent replays depends on the *second* request losing an INSERT, which is atomic.
    Checking with a SELECT first and inserting after leaves a window where both callers see
    nothing and both proceed — exactly the duplicate the header exists to prevent.
    """

    __tablename__ = "idempotency_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Hash of the request body: a replay with a different payload is a client bug and must
    # be rejected, never answered with the first request's stored response.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        _enum_column(IdempotencyStatus, name="idempotency_status"),
        default=IdempotencyStatus.IN_PROGRESS,
        nullable=False,
    )
    response_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Lets the cleanup job find expired rows without scanning the table.
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )
